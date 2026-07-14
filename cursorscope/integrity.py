from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .agentkv import AgentKvReport, build_agentkv_report
from .analyze import human_bytes, load_headers
from .db import PageStats, open_ro, page_stats, quick_check
from .scan import get_storage_scan
from .workspace import (
    WorkspaceResolver,
    load_composer_metadata,
    resolve_repo,
    resolve_repo_from_fallback,
)

KNOWN_PREFIXES = {
    "bubbleId",
    "checkpointId",
    "composerData",
    "messageRequestContext",
    "codeBlockDiff",
    "codeBlockPartialInlineDiffFates",
    "agentKv",
    "composer.*",
    "ofsContent",
    "inlineDiff*",
    "composerVirtualRowHeights:",
}


@dataclass
class PrefixRow:
    prefix: str
    count: int
    bytes: int
    known: bool


@dataclass
class OrphanSession:
    composer_id: str
    bytes: int
    has_composer_data: bool
    breakdown: dict[str, int]
    repo: str | None = None
    repo_confidence: str = "low"


@dataclass
class MissingPath:
    path: str
    source: str
    composer_id: str | None = None
    name: str | None = None


@dataclass
class IntegrityReport:
    source: str
    snapshot: str
    quick_check: str
    pages: PageStats
    summary: dict[str, int | str | float]
    orphan_sessions: list[OrphanSession]
    headers_without_composer_data: list[str]
    composer_data_without_storage: list[str]
    orphaned_object_keys: dict[str, int]
    missing_paths: list[MissingPath]
    unknown_prefixes: list[PrefixRow]
    agentkv: AgentKvReport | None = None
    notes: list[str] = field(default_factory=list)



def find_missing_paths(
    headers: dict[str, dict],
    resolver: WorkspaceResolver,
) -> list[MissingPath]:
    missing: list[MissingPath] = []
    seen: set[tuple[str, str]] = set()

    for composer_id, header in headers.items():
        repo, confidence = resolve_repo(header, resolver)
        name = header.get("name")
        key = (repo, composer_id)
        if key in seen:
            continue
        seen.add(key)

        if repo in ("(unknown)",) or not repo:
            continue

        if repo.startswith("workspace:"):
            workspace_id = repo[len("workspace:") :]
            ws_json = resolver.storage_dir / workspace_id / "workspace.json"
            if not ws_json.is_file():
                missing.append(
                    MissingPath(
                        path=repo,
                        source="workspace.json missing",
                        composer_id=composer_id,
                        name=name,
                    )
                )
            continue

        if confidence == "high" and not Path(repo).exists():
            missing.append(
                MissingPath(
                    path=repo,
                    source="repo path missing on disk",
                    composer_id=composer_id,
                    name=name,
                )
            )
    return missing


def build_integrity_report(
    db_path: Path,
    *,
    source: Path,
    top_n: int = 15,
    resolver: WorkspaceResolver | None = None,
    scan_agentkv: bool = True,
    progress: bool = False,
    use_cache: bool = True,
    fast: bool = False,
) -> IntegrityReport:
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
        )
        composer_data_ids = storage.composer_data_ids
        by_composer = storage.by_composer
        composer_breakdown = storage.composer_breakdown
        prefix_inventory = [
            PrefixRow(prefix=label, count=count, bytes=size, known=known)
            for label, count, size, known in storage.prefix_rows(KNOWN_PREFIXES)
        ]
        orphaned_objects = dict(storage.orphaned_object_keys)
        missing_paths = find_missing_paths(headers, resolver)

        header_ids = set(headers)
        storage_ids = set(by_composer)
        headers_without_data = sorted(header_ids - composer_data_ids)
        storage_without_headers = storage_ids - header_ids
        composer_data_without_storage = sorted(composer_data_ids - storage_ids)

        orphan_sessions = []
        for cid in storage_without_headers:
            meta = (
                load_composer_metadata(conn, cid)
                if cid in composer_data_ids
                else {}
            )
            repo, confidence = (
                resolve_repo_from_fallback(meta, resolver) if meta else (None, "low")
            )
            if repo == "(unknown)":
                repo = None
            orphan_sessions.append(
                OrphanSession(
                    composer_id=cid,
                    bytes=by_composer[cid],
                    has_composer_data=cid in composer_data_ids,
                    breakdown=dict(composer_breakdown.get(cid, {})),
                    repo=repo,
                    repo_confidence=confidence,
                )
            )
        orphan_sessions.sort(key=lambda row: row.bytes, reverse=True)

        pages = page_stats(conn)
        qc = quick_check(conn)
        unknown_prefixes = (
            [] if fast else [row for row in prefix_inventory if not row.known]
        )

        agentkv_report: AgentKvReport | None = None
        if scan_agentkv:
            agentkv_report = build_agentkv_report(conn, progress=progress)

        orphan_bytes = sum(row.bytes for row in orphan_sessions)
        orphan_attributed_bytes = sum(
            row.bytes for row in orphan_sessions if row.repo
        )
        notes: list[str] = []

        if fast:
            notes.append(
                "Fast mode: skipped agentKv scan and unknown-prefix detail table."
            )

        if pages.reclaimable_ratio < 0.01:
            notes.append(
                "Reclaimable SQLite pages are negligible; database size is dominated by live data."
            )
        elif pages.reclaimable_ratio >= 0.05:
            notes.append(
                f"About {pages.reclaimable_ratio * 100:.1f}% of file bytes are reclaimable freelist pages."
            )

        if orphan_sessions:
            notes.append(
                "Sessions without headers are usually historical; header index only tracks recent composers."
            )
            if orphan_attributed_bytes:
                notes.append(
                    "Repo labels for orphan sessions fall back to composerData (trackedGitRepos, workspaceIdentifier, or fsPath git-root inference); confidence is medium at best."
                )

        if any(orphaned_objects.values()):
            notes.append(
                "Orphaned bubble/checkpoint counts may include Cursor-internal references; diagnose only."
            )

        if agentkv_report and agentkv_report.unreferenced_bytes > 0:
            notes.append(
                "Most agentKv blobs lack structural session references in cursorDiskKV."
            )

        summary: dict[str, int | str | float] = {
            "quick_check": qc,
            "file_bytes": pages.file_bytes,
            "reclaimable_bytes": pages.reclaimable_bytes,
            "reclaimable_ratio": pages.reclaimable_ratio,
            "cursor_disk_kv_rows": storage.kv_row_count,
            "composer_data_rows": len(composer_data_ids),
            "header_rows": len(headers),
            "sessions_with_storage": len(storage_ids),
            "sessions_without_headers": len(storage_without_headers),
            "sessions_without_headers_bytes": orphan_bytes,
            "sessions_without_headers_attributed_bytes": orphan_attributed_bytes,
            "headers_without_composer_data": len(headers_without_data),
            "composer_data_without_storage": len(composer_data_without_storage),
            "missing_paths": len(missing_paths),
            "unknown_prefix_count": sum(
                1 for row in prefix_inventory if not row.known
            ),
            "orphaned_bubble_keys": orphaned_objects.get("bubbleId", 0),
            "orphaned_checkpoint_keys": orphaned_objects.get("checkpointId", 0),
        }
        if agentkv_report:
            summary["agentkv_unreferenced_blobs"] = agentkv_report.unreferenced_blobs
            summary["agentkv_unreferenced_bytes"] = agentkv_report.unreferenced_bytes

        return IntegrityReport(
            source=str(source),
            snapshot=str(db_path),
            quick_check=qc,
            pages=pages,
            summary=summary,
            orphan_sessions=orphan_sessions[:top_n],
            headers_without_composer_data=headers_without_data,
            composer_data_without_storage=composer_data_without_storage[:top_n],
            orphaned_object_keys=orphaned_objects,
            missing_paths=missing_paths,
            unknown_prefixes=unknown_prefixes,
            agentkv=agentkv_report,
            notes=notes,
        )
    finally:
        conn.close()


def print_integrity_report(report: IntegrityReport) -> None:
    pages = report.pages
    print("CursorScope integrity")
    print(f"source:   {report.source}")
    print(f"snapshot: {report.snapshot}")
    print(f"health:   quick_check={report.quick_check}")
    print(
        f"pages:    {human_bytes(pages.file_bytes)} total, "
        f"{human_bytes(pages.reclaimable_bytes)} reclaimable ({pages.reclaimable_ratio * 100:.1f}%)"
    )
    print()

    print("Summary")
    labels = {
        "cursor_disk_kv_rows": "cursorDiskKV rows",
        "composer_data_rows": "composerData rows",
        "header_rows": "composer header rows",
        "sessions_with_storage": "sessions with storage",
        "sessions_without_headers": "sessions without headers",
        "sessions_without_headers_bytes": "storage in sessions without headers",
        "headers_without_composer_data": "headers without composerData",
        "composer_data_without_storage": "composerData without other storage",
        "missing_paths": "missing repo/workspace paths",
        "unknown_prefix_count": "unknown prefix types",
        "orphaned_bubble_keys": "bubble keys without composerData",
        "orphaned_checkpoint_keys": "checkpoint keys without composerData",
        "agentkv_unreferenced_blobs": "unreferenced agentKv blobs",
        "agentkv_unreferenced_bytes": "unreferenced agentKv bytes",
    }
    for key, label in labels.items():
        if key not in report.summary:
            continue
        value = report.summary[key]
        if key.endswith("_bytes"):
            print(f"  {label:<42} {human_bytes(int(value)):>10}")
        elif isinstance(value, float):
            print(f"  {label:<42} {value:>10.4f}")
        else:
            print(f"  {label:<42} {value!s:>10}")
    print()

    if report.orphan_sessions:
        print(f"Top sessions without headers ({report.summary['sessions_without_headers']} total)")
        print(f"{'Size':>10}  {'Data':<4}  {'Conf':<6}  {'Repo / Composer ID'}")
        print("-" * 90)
        for row in report.orphan_sessions:
            has = "yes" if row.has_composer_data else "no"
            label = row.repo or row.composer_id
            print(
                f"{human_bytes(row.bytes):>10}  {has:<4}  {row.repo_confidence:<6}  {label}"
            )
            if row.repo:
                print(f"{'':>10}        {row.composer_id}")
        print()

    if report.headers_without_composer_data:
        print(f"Headers without composerData ({len(report.headers_without_composer_data)})")
        for cid in report.headers_without_composer_data[:10]:
            print(f"  {cid}")
        if len(report.headers_without_composer_data) > 10:
            print(f"  ... and {len(report.headers_without_composer_data) - 10} more")
        print()

    if report.composer_data_without_storage:
        total = int(report.summary.get("composer_data_without_storage", 0))
        print(f"composerData without bubbles/checkpoints ({total})")
        for cid in report.composer_data_without_storage:
            print(f"  {cid}")
        if total > len(report.composer_data_without_storage):
            print(f"  ... and {total - len(report.composer_data_without_storage)} more")
        print()

    orphaned = report.orphaned_object_keys
    if any(orphaned.values()):
        print("Orphaned object keys (no composerData row for composer id)")
        for kind, count in sorted(orphaned.items(), key=lambda item: -item[1]):
            if count:
                print(f"  {kind:<36} {count:>8,}")
        print()

    if report.missing_paths:
        print(f"Missing paths ({len(report.missing_paths)})")
        print(f"{'Source':<28}  Path")
        print("-" * 90)
        for item in report.missing_paths[:20]:
            label = item.name or item.composer_id or ""
            print(f"{item.source:<28}  {item.path}")
            if label:
                print(f"{'':28}  ({label})")
        if len(report.missing_paths) > 20:
            print(f"  ... and {len(report.missing_paths) - 20} more")
        print()

    if report.unknown_prefixes:
        print("Unknown / unclassified prefixes")
        print(f"{'Prefix':<42} {'Count':>8}  {'Size':>10}")
        print("-" * 64)
        for row in report.unknown_prefixes:
            print(f"{row.prefix:<42} {row.count:>8,}  {human_bytes(row.bytes):>10}")
        print()

    if report.agentkv:
        akv = report.agentkv
        print("AgentKv (diagnostic)")
        print(f"  total:         {akv.total_blobs:,} blobs ({human_bytes(akv.total_bytes)})")
        print(f"  referenced:    {akv.referenced_blobs:,} blobs ({human_bytes(akv.referenced_bytes_unique)})")
        print(f"  unreferenced:  {akv.unreferenced_blobs:,} blobs ({human_bytes(akv.unreferenced_bytes)})")
        print()

    if report.notes:
        print("Notes")
        for note in report.notes:
            print(f"  - {note}")


def integrity_report_to_dict(report: IntegrityReport) -> dict:
    from dataclasses import asdict

    data = asdict(report)
    data["pages"] = asdict(report.pages)
    if report.agentkv:
        data["agentkv"] = asdict(report.agentkv)
    return data
