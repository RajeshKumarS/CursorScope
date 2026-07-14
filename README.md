# CursorScope

Read-only CLI and desktop UI for understanding Cursor's `state.vscdb` — Composer sessions, storage attribution, and database health.

CursorScope is a **database interpreter**, not a SQLite browser. It answers questions like:

- Why is my `state.vscdb` so large?
- Which repositories and sessions consume the most space?
- Which files participated in a session?
- Is the database healthy?

**It never writes to your live database.** By default it reads `state.vscdb` **in place** with no disk copy — important when the file is already huge and disk is tight.

## Requirements

- Python 3.9+
- Windows, macOS, or Linux
- Cursor installed (uses `%APPDATA%\Cursor\...` on Windows by default)

## Install

```bash
git clone <repo-url>
cd CursorScope
pip install -e .
```

Verify:

```bash
python -m cursorscope --version
```

Build the desktop UI (required for `cursorscope serve`):

```bash
cd desktop && npm install && npm run build
```

## Quick start

```bash
# Database overview (reads live DB, no copy)
cursorscope

# Session drill-down (UUID or unique prefix)
cursorscope session <composer-id-prefix>

# Health and orphan diagnostics
cursorscope integrity

# Historical sessions not in the header index
cursorscope sessions --no-header
```

## Snapshots and disk space

Many users first notice `state.vscdb` when it grows to multiple GB and disk space runs low. **CursorScope does not copy the database by default.**

| Mode | Extra disk used | When to use |
|------|-----------------|-------------|
| **Default (live read-only)** | **0** | Normal diagnosis; best when disk is tight |
| `--snapshot PATH` | 0 | You already have a copy elsewhere |
| `--ephemeral` | ~size of DB during run | Point-in-time copy; deleted on exit |
| `--cached-snapshot` | ~size of DB until cleanup | Repeat runs without re-copying; opt-in only |

```bash
cursorscope --ephemeral          # one-time copy
cursorscope --cached-snapshot    # reusable cache under ~/.cursorscope/snapshots/
cursorscope cleanup              # remove cached snapshots, scan cache, old history
cursorscope cleanup --backups    # remove prune backups (see spike/README.md)
```

## Commands

| Command | Description |
|---------|-------------|
| `cursorscope` / `cursorscope analyze` | Storage by type, top repos/sessions, agentKv summary |
| `cursorscope session <id>` | One session: files, bubbles, checkpoints, storage breakdown |
| `cursorscope sessions` | List sessions by size |
| `cursorscope sessions --no-header` | Sessions with storage but no `composer.composerHeaders` entry |
| `cursorscope integrity` | Orphans, missing paths, unknown prefixes, reclaimable pages |
| `cursorscope coverage` | Orphan session composerData repo fallback coverage |
| `cursorscope timeline` | Session timeline from composer metadata |
| `cursorscope growth` | Compare vs last scan and Cursor backup file |
| `cursorscope history` | List saved auto-scan summaries |
| `cursorscope checkpoint create <label>` | Save a named storage summary (~KB, no DB copy) |
| `cursorscope checkpoint list` | List checkpoints |
| `cursorscope checkpoint diff <from> <to>` | Compare two checkpoints |
| `cursorscope checkpoint delete <ref>` | Delete a checkpoint |
| `cursorscope plan-remove --repo <path>` | Preview repo-scoped removal (read-only) |
| `cursorscope cleanup` | Delete cached snapshots, scan cache, old history; optional `--backups` |
| `cursorscope serve` | Local HTTP API + built desktop UI |

### Common flags

| Flag | Description |
|------|-------------|
| `--snapshot PATH` | Analyze an existing copy (no new copy created) |
| `--cached-snapshot` | Opt-in: copy to `~/.cursorscope/snapshots/` and reuse until live DB changes |
| `--ephemeral` | Opt-in: copy for this run only, deleted on exit |
| `--src PATH` | Path to live `state.vscdb` |
| `--workspace-dir PATH` | Cursor `workspaceStorage` directory |
| `--skip-agentkv` / `--fast` | Skip agentKv blob attribution scan |
| `--no-history` | Do not save scan summary to `~/.cursorscope/history/` |
| `--no-cache` | Force a fresh cursorDiskKV scan |
| `--top N` | Limit ranked output |
| `--json` | Machine-readable JSON |

## Data locations

| Store | Default path (Windows) |
|-------|------------------------|
| Global state | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` |
| Workspace map | `%APPDATA%\Cursor\User\workspaceStorage\<id>\workspace.json` |
| Scan cache | `~/.cursorscope/cache/` |
| Scan history | `~/.cursorscope/history/` |
| Prune backups (ops) | `~/.cursorscope/backups/` |

## Performance

`analyze` scans all `cursorDiskKV` rows once (~1–2 minutes on a 200k-row / multi-GB database). Results are cached in `~/.cursorscope/cache/` and reused across commands while the DB file is unchanged.

Use `--fast` or `--skip-agentkv` for a quicker overview. The CLI and UI show scan progress during long runs.

## Privacy

`state.vscdb` can contain code snippets, file paths, and conversation content. CursorScope:

- Runs locally
- Does not send telemetry
- Summarizes payloads (sizes, paths, metadata) rather than dumping conversation text in reports

Terminal output and the UI may still show sensitive paths or session titles from your database.

## Desktop UI

**Repositories** and **Timeline** views with drill-down to sessions and session detail. Includes growth/checkpoint panels, scan progress, and disk-space tips.

```bash
# Terminal 1: API (serves built UI from cursorscope/static/)
python -m cursorscope serve

# Terminal 2: dev UI with hot reload
cd desktop && npm run dev
```

Open http://127.0.0.1:5173 in development or http://127.0.0.1:8765 after `npm run build`.

For a native window, see `desktop/README.md` (Tauri; requires Rust).

## Experimental ops

Destructive repo pruning lives under `spike/` (`prune_repos.py`), not the main CLI. Preview with `cursorscope plan-remove`. See `spike/README.md`.

## Project layout

```text
cursorscope/          # Python package (CLI, API, analysis)
desktop/              # Vite + TypeScript UI
spike/                # Research notes + experimental scripts
```

See `spike/RESEARCH.md` for schema and attribution notes.

## Limitations

- **Repo attribution** is high-confidence only for sessions in `composer.composerHeaders`; most historical storage may lack headers.
- **agentKv blobs** often lack structural session references; use Cursor's GC command after cleanup.
- **Cursor's "Delete Old Chats"** only covers the header index — not orphan KV storage (see research notes).

Diagnose only — the supported product does not mutate `state.vscdb`.
