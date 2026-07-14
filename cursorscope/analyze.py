from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .agentkv import AgentKvReport, build_agentkv_report
from .db import PageStats, open_ro, page_stats, quick_check
from .scan import ProgressReporter, classify_key, get_storage_scan
from .workspace import WorkspaceResolver, load_composer_metadata, resolve_repo, resolve_repo_from_fallback


def human_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(f) < 1024 or unit == "TB":
            return f"{f:.2f} {unit}"
        f /= 1024
    return f"{n} B"


def repo_label(composer: dict, resolver: WorkspaceResolver | None = None) -> str:
    label, _ = resolve_repo(composer, resolver)
    return label


def load_headers(conn: sqlite3.Connection) -> dict[str, dict]:
    row = conn.execute(
        'SELECT value FROM ItemTable WHERE key = ?',
        ("composer.composerHeaders",),
    ).fetchone()
    if not row:
        return {}
    raw = row[0]
    text = raw if isinstance(raw, str) else raw.decode("utf-8")
    obj = json.loads(text)
    return {
        c["composerId"]: c
        for c in obj.get("allComposers") or []
        if c.get("composerId")
    }


@dataclass
class TypeRow:
    prefix: str
    count: int
    bytes: int


@dataclass
class SessionRow:
    composer_id: str
    bytes: int
    name: str | None = None
    subtitle: str | None = None
    repo: str | None = None
    confidence: str = "none"
    breakdown: dict[str, int] = field(default_factory=dict)


@dataclass
class RepoRow:
    repo: str
    bytes: int
    session_count: int
    confidence: str = "high"


@dataclass
class OtherStorageRow:
    label: str
    bytes: int
    count: int = 0
    description: str = ""


SESSION_KINDS = (
    "bubbleId",
    "checkpointId",
    "composerData",
    "messageRequestContext",
    "codeBlockDiff",
    "codeBlockPartialInlineDiffFates",
)


def build_other_storage_breakdown(
    *,
    by_type: Counter[str],
    by_type_count: Counter[str],
    composer_breakdown: dict[str, Counter[str]],
    cursor_disk_kv_bytes: int,
    total_session_bytes: int,
    orphaned_object_keys: Counter[str],
    unattributed: dict[str, int],
) -> list[OtherStorageRow]:
    rows: list[OtherStorageRow] = []

    agentkv = int(by_type.get("agentKv", 0))
    if agentkv:
        rows.append(
            OtherStorageRow(
                label="agentKv blobs",
                bytes=agentkv,
                count=int(by_type_count.get("agentKv", 0)),
                description=(
                    "Binary blobs in agentKv; most lack structural links to Composer sessions. "
                    "This is on-disk Cursor storage, not a process memory leak."
                ),
            )
        )

    composer_star = int(by_type.get("composer.*", 0))
    if composer_star:
        rows.append(
            OtherStorageRow(
                label="composer.* metadata",
                bytes=composer_star,
                count=int(by_type_count.get("composer.*", 0)),
                description="Global composer index and UI state outside per-session rows.",
            )
        )

    unlinked_bytes = 0
    unlinked_keys = 0
    for kind in SESSION_KINDS:
        total = int(by_type.get(kind, 0))
        attributed = sum(int(cb.get(kind, 0)) for cb in composer_breakdown.values())
        unlinked_bytes += max(0, total - attributed)
        unlinked_keys += int(orphaned_object_keys.get(kind, 0))
    if unlinked_bytes:
        rows.append(
            OtherStorageRow(
                label="Unlinked session objects",
                bytes=unlinked_bytes,
                count=unlinked_keys,
                description=(
                    "bubbleId/checkpointId/etc. without a composerData row or with an orphan composer id."
                ),
            )
        )

    for label, desc in (
        ("ofsContent", "Cursor offline-sync payloads."),
        ("inlineDiff*", "Inline diff state stored outside session attribution."),
        ("(other)", "Keys that did not match a known prefix pattern."),
        ("(invalid key)", "Malformed cursorDiskKV keys."),
    ):
        size = int(by_type.get(label, 0))
        if size:
            rows.append(
                OtherStorageRow(
                    label=label,
                    bytes=size,
                    count=int(by_type_count.get(label, 0)),
                    description=desc,
                )
            )

    other_prefix = int(unattributed.get("other_prefixes", 0))
    if other_prefix:
        rows.append(
            OtherStorageRow(
                label="Rare / unknown prefixes",
                bytes=other_prefix,
                count=0,
                description="Uncommon key prefixes not mapped to known Composer object types.",
            )
        )

    categorized = sum(row.bytes for row in rows)
    other_total = max(0, cursor_disk_kv_bytes - total_session_bytes)
    remainder = max(0, other_total - categorized)
    if remainder:
        rows.append(
            OtherStorageRow(
                label="Other uncategorized KV",
                bytes=remainder,
                description="Residual cursorDiskKV bytes not in the categories above.",
            )
        )

    rows.sort(key=lambda row: row.bytes, reverse=True)
    return rows


@dataclass
class AnalysisReport:
    source: str
    snapshot: str
    quick_check: str
    pages: PageStats
    item_table_rows: int
    kv_rows: int
    composer_data_count: int
    header_count: int
    storage_by_type: list[TypeRow]
    top_sessions: list[SessionRow]
    top_repos: list[RepoRow]
    unattributed: dict[str, int]
    integrity: dict[str, int | str]
    repos_attributed_bytes: int = 0
    unknown_session_bytes: int = 0
    cursor_disk_kv_bytes: int = 0
    other_storage_breakdown: list[OtherStorageRow] = field(default_factory=list)
    agentkv: AgentKvReport | None = None



def build_report(
    db_path: Path,
    *,
    source: Path,
    top_n: int = 15,
    top_n_repos: int | None = None,
    resolver: WorkspaceResolver | None = None,
    scan_agentkv: bool = True,
    progress: bool = False,
    use_cache: bool = True,
    fast: bool = False,
    on_progress: ProgressReporter | None = None,
) -> AnalysisReport:
    if fast:
        scan_agentkv = False
    resolver = resolver or WorkspaceResolver()
    conn = open_ro(db_path)
    try:
        headers = load_headers(conn)
        storage = get_storage_scan(
            conn,
            db_path,
            progress=progress,
            use_cache=use_cache,
            on_progress=on_progress,
        )
        by_type = storage.by_type
        by_type_count = storage.by_type_count
        by_composer = storage.by_composer
        composer_breakdown = storage.composer_breakdown
        composer_data_ids = storage.composer_data_ids

        item_rows = int(conn.execute('SELECT COUNT(*) FROM ItemTable').fetchone()[0])
        kv_rows = storage.kv_row_count
        composer_data_count = len(composer_data_ids)
        by_repo = Counter()
        repo_sessions: dict[str, set[str]] = {}
        repo_confidence: dict[str, str] = {}
        attributed_bytes = 0
        orphan_bytes = 0
        orphan_attributed_bytes = 0

        if on_progress:
            on_progress(0, 1, "Finishing analysis…", finishing=True)

        sessions: list[SessionRow] = []
        composer_items = list(by_composer.most_common())
        total_composers = len(composer_items)
        for index, (composer_id, total) in enumerate(composer_items, start=1):
            if on_progress and (
                index == 1
                or index == total_composers
                or index % 25 == 0
            ):
                on_progress(
                    index,
                    max(total_composers, 1),
                    "Finishing analysis…",
                    finishing=True,
                )
            header = headers.get(composer_id)
            meta = header or (
                load_composer_metadata(conn, composer_id)
                if composer_id in composer_data_ids
                else {}
            )

            if header:
                repo, confidence = resolve_repo(header, resolver)
                by_repo[repo] += total
                repo_sessions.setdefault(repo, set()).add(composer_id)
                repo_confidence[repo] = confidence
                attributed_bytes += total
            else:
                orphan_bytes += total
                confidence = "low"
                repo = None
                if meta:
                    repo, confidence = resolve_repo_from_fallback(meta, resolver)
                    if repo and repo != "(unknown)":
                        by_repo[repo] += total
                        repo_sessions.setdefault(repo, set()).add(composer_id)
                        repo_confidence[repo] = confidence
                        attributed_bytes += total
                        orphan_attributed_bytes += total

            breakdown = dict(composer_breakdown.get(composer_id, {}))
            sessions.append(
                SessionRow(
                    composer_id=composer_id,
                    bytes=total,
                    name=meta.get("name"),
                    subtitle=meta.get("subtitle"),
                    repo=repo,
                    confidence=confidence,
                    breakdown=breakdown,
                )
            )

        agentkv_report: AgentKvReport | None = None
        if scan_agentkv:
            agentkv_report = build_agentkv_report(conn, progress=progress)
            for row in sessions:
                extra = agentkv_report.attributed_by_composer.get(row.composer_id, 0)
                if extra:
                    row.breakdown["agentKv"] = extra
                    row.bytes += extra
                row.breakdown = dict(sorted(row.breakdown.items(), key=lambda item: -item[1]))
            sessions.sort(key=lambda row: row.bytes, reverse=True)

            for composer_id, extra in agentkv_report.attributed_by_composer.items():
                header = headers.get(composer_id)
                if header:
                    repo, _ = resolve_repo(header, resolver)
                    by_repo[repo] += extra

        unattributed = {
            "agentKv_unreferenced": (
                agentkv_report.unreferenced_bytes if agentkv_report else by_type.get("agentKv", 0)
            ),
            "agentKv_total": by_type.get("agentKv", 0),
            "composer.content": (
                agentkv_report.composer_content_bytes if agentkv_report else 0
            ),
            "composer.* (excl. content)": (
                max(0, by_type.get("composer.*", 0) - agentkv_report.composer_content_bytes)
                if agentkv_report
                else by_type.get("composer.*", 0)
            ),
            "other_prefixes": sum(
                size
                for label, size in by_type.items()
                if label
                not in {
                    "bubbleId",
                    "checkpointId",
                    "composerData",
                    "messageRequestContext",
                    "codeBlockDiff",
                    "codeBlockPartialInlineDiffFates",
                    "agentKv",
                    "composer.*",
                }
            ),
            "sessions_without_headers": orphan_bytes,
            "sessions_without_headers_attributed": orphan_attributed_bytes,
        }

        repo_limit = top_n_repos if top_n_repos is not None else top_n
        top_repos = [
            RepoRow(
                repo=repo,
                bytes=size,
                session_count=len(repo_sessions.get(repo, set())),
                confidence=repo_confidence.get(repo, "medium"),
            )
            for repo, size in by_repo.most_common(repo_limit)
        ]
        repos_attributed_bytes = sum(by_repo.values())
        total_session_bytes = sum(by_composer.values())
        unknown_session_bytes = total_session_bytes - repos_attributed_bytes
        cursor_disk_kv_bytes = sum(by_type.values())
        other_storage_breakdown = build_other_storage_breakdown(
            by_type=by_type,
            by_type_count=by_type_count,
            composer_breakdown=composer_breakdown,
            cursor_disk_kv_bytes=cursor_disk_kv_bytes,
            total_session_bytes=total_session_bytes,
            orphaned_object_keys=storage.orphaned_object_keys,
            unattributed=unattributed,
        )

        header_ids = set(headers)
        composer_ids = {cid for cid in by_composer}
        integrity = {
            "composer_data_rows": composer_data_count,
            "header_rows": len(headers),
            "sessions_without_headers": len(composer_ids - header_ids),
            "headers_without_composer_data": len(header_ids - composer_ids),
        }

        return AnalysisReport(
            source=str(source),
            snapshot=str(db_path),
            quick_check=quick_check(conn),
            pages=page_stats(conn),
            item_table_rows=item_rows,
            kv_rows=kv_rows,
            composer_data_count=composer_data_count,
            header_count=len(headers),
            storage_by_type=[
                TypeRow(prefix=label, count=by_type_count[label], bytes=size)
                for label, size in by_type.most_common()
            ],
            top_sessions=sessions[:top_n],
            top_repos=top_repos,
            unattributed=unattributed,
            integrity=integrity,
            repos_attributed_bytes=repos_attributed_bytes,
            unknown_session_bytes=unknown_session_bytes,
            cursor_disk_kv_bytes=cursor_disk_kv_bytes,
            other_storage_breakdown=other_storage_breakdown,
            agentkv=agentkv_report,
        )
    finally:
        conn.close()
