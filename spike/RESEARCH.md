# CursorScope — Research Notes

Public summary of reverse-engineering Cursor's global `state.vscdb`. Machine-specific paths, session titles, and repo names belong in `RESEARCH.local.md` (gitignored).

Validated against a multi-GB production-class database on Windows. Payload contents are not reproduced here.

---

## Executive findings

1. **Almost all bulk storage lives in one table:** `cursorDiskKV` (~200k+ rows). `ItemTable` (~360 rows) holds app/UI state and the **Composer header index**, not conversation bulk.
2. **Storage is dominated by checkpoints, then agent blobs, then bubbles.** Reclaimable SQLite free pages are typically ~0%; growth is live data, not freelist bloat.
3. **Repo-first UI is feasible for active composers** via `composer.composerHeaders` → `workspaceIdentifier` / `trackedGitRepos`, cross-checked with `workspaceStorage/<id>/workspace.json`.
4. **Header coverage is incomplete:** on the test database, ~360 `composerData:*` rows vs ~40 headers. A large share of composer-keyed storage has no header metadata → high-confidence repo labels cover only part of the database.
5. **`agentKv` (~1 GB class) is content-addressed** (`agentKv:blob:<sha256>`). Blobs are referenced from bubble/checkpoint payloads; attribution requires graph join, not key prefix alone.

---

## Data locations

| Store | Path | Role |
| --- | --- | --- |
| Global state | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` | Composer/agent history bulk |
| Global backup | `state.vscdb.backup` | Older snapshot (when present) |
| Workspace state | `workspaceStorage/<hash>/state.vscdb` | Per-folder VS Code state (small) |
| Workspace map | `workspaceStorage/<hash>/workspace.json` | `{ "folder": "file:///..." }` |

CursorScope treats global `state.vscdb` as the primary analytics target. Workspace DBs are secondary (folder URI resolution, local UI state).

---

## Safe open

| Method | Result |
| --- | --- |
| Live open read-only | Default in CursorScope when disk is tight; no extra copy |
| File copy of `state.vscdb` + `-wal` + `-shm` | Point-in-time snapshot; works while Cursor is running |
| Open copy `mode=ro` + `PRAGMA query_only=ON` | OK |
| `PRAGMA quick_check` | Typically `ok` |
| Freelist | Usually ~0% reclaimable on large production DBs |

Reclaimable pages rarely explain multi-GB size; Storage Analyzer must focus on object attribution.

---

## Schema (global DB)

```text
ItemTable(key TEXT UNIQUE, value BLOB)       -- ~360 rows
cursorDiskKV(key TEXT UNIQUE, value BLOB)  -- ~200k+ rows
```

The effective schema is **key prefixes** inside `cursorDiskKV`, not normalized relational tables.

---

## Object inventory (`cursorDiskKV`)

Approximate value-byte distribution on a large test database:

| Prefix / class | ≈ Share | Notes |
| --- | --- | --- |
| `checkpointId:<composer>:<checkpoint>` | ~1.6 GB | Largest class |
| `agentKv:blob:<sha256>` | ~1.0 GB | Content-addressed; not composer-keyed |
| `bubbleId:<composer>:<bubble>` | ~700 MB | Conversation turns |
| `composer.*` | ~165 MB | Secondary composer keys |
| `codeBlockDiff:*` | ~65 MB | Diff payloads |
| `composerData:<composer>` | ~44 MB | Session document |
| Other prefixes | smaller | `messageRequestContext`, `inlineDiff*`, etc. |

Notable `ItemTable` keys: `composer.composerHeaders` (recent composer index), `history.recentlyOpenedPathsList`, `aiCodeTrackingLines`.

---

## Conceptual model

```text
workspaceStorage/<id>/workspace.json  →  folder URI
                ↑
ItemTable: composer.composerHeaders
  allComposers[]: composerId, name, workspaceIdentifier, trackedGitRepos, …
                ↓
cursorDiskKV
  composerData / bubbleId / checkpointId / codeBlockDiff / …
                ↓
  agentKv:blob:<hash>   ← referenced from bubble/checkpoint values
```

File paths appear in headers and inside bubble/checkpoint JSON. Blobs are shared by hash (many-to-many).

---

## Attribution

### Sessions

Key prefixes embed `composerId`. Ranking sessions by size is trustworthy.

### Repositories

**High confidence** — composer in `composer.composerHeaders` with `trackedGitRepos` or folder URI.

**Medium confidence** — `composerData` fallback (path extraction from `fsPath`, `repoPath`, `workspaceIdentifier`, `codeBlockData`, etc.) when no header exists.

**Low / unattributed** — no repo signals, or `agentKv` not yet joined to a session.

Never present high-confidence repo totals as “100% of database.”

### composerData repo fallback

For sessions without a header entry, CursorScope extracts repo hints from `composerData:*`:

1. Full JSON parse when ≤ 2 MB; otherwise scan the first 500 KB.
2. Nested field walk + regex fallback.
3. Project-root inference from `fsPath` (`.git`, `package.json`, etc.).

On the test database, most orphan bytes received a medium-confidence repo label via fallback; a handful of very large orphans dominate the total.

### agentKv

Structural `agentKv:blob:HASH` references in session payloads are rare. Most agentKv bytes appear unreferenced with current decoding — run Cursor's **Developer: GC Agent KV Blobs** after session cleanup.

Related store: `composer.content.<hash>` (overlapping hashes, different on-disk representation).

Content-addressing confirmed: `SHA-256(value)` matches hash suffix in keys.

---

## Integrity signals

| Signal | Typical observation |
| --- | --- |
| Orphan sessions | Storage in KV but no `composer.composerHeaders` entry |
| Missing folder on disk | Check `workspaceIdentifier.uri.fsPath` / `workspace.json` |
| Unknown prefixes | Small `(other)` bucket; gate via schema inventory |
| Freelist bloat | Not the issue on large test DBs |
| Shared blob refs | Same hash referenced by multiple bubbles (expected) |

---

## Performance (order of magnitude)

| Operation | Time |
| --- | --- |
| Prefix inventory scan (all KV rows) | ~10–20 s |
| Storage-by-composer scan | ~15–30 s |
| Full JSON parse of all bubbles | Too slow; decode on drill-down only |

Scan results are cached under `~/.cursorscope/cache/` while the DB file is unchanged.

---

## Risks

1. **Stale headers** — most historical size may sit outside the header index (invisible to Cursor's age-based delete).
2. **agentKv opacity** — blob→session join is incomplete.
3. **Sensitive paths and prompts** in DB — never commit snapshots or `RESEARCH.local.md`.
4. **Cursor still writing** — live analysis is a moment in time; opt-in snapshot for point-in-time consistency.

---

## Local artifacts

```text
spike/
  RESEARCH.md         # this file (public)
  RESEARCH.local.md   # gitignored — machine-specific case study
  snapshots/          # gitignored — DB copies + JSON reports
```

Experimental ops scripts (`prune_repos.py`, probes) live here; stable logic moves into `cursorscope/`.
