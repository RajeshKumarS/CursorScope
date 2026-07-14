from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .analyze import human_bytes, load_headers
from .scan import get_storage_scan
from .workspace import WorkspaceResolver, load_composer_metadata, resolve_repo_from_fallback


@dataclass
class FallbackCoverageReport:
    orphan_sessions: int
    orphan_bytes: int
    with_composer_data: int
    attributed_sessions: int
    attributed_bytes: int
    coverage_ratio: float
    via_repo_path: int
    via_workspace: int
    via_fs_path: int


def build_fallback_coverage_report(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    resolver: WorkspaceResolver | None = None,
    use_cache: bool = True,
) -> FallbackCoverageReport:
    """Measure composerData repo fallback coverage for orphan sessions."""
    resolver = resolver or WorkspaceResolver()
    headers = load_headers(conn)
    header_ids = set(headers)
    storage = get_storage_scan(conn, db_path, use_cache=use_cache, progress=False)

    total = 0
    total_bytes = 0
    with_composer_data = 0
    attributed = 0
    attributed_bytes = 0
    via_repo_path = 0
    via_workspace = 0
    via_fs_path = 0

    for composer_id, nbytes in storage.by_composer.items():
        if composer_id in header_ids:
            continue
        total += 1
        total_bytes += nbytes
        if composer_id in storage.composer_data_ids:
            with_composer_data += 1
        meta = (
            load_composer_metadata(conn, composer_id)
            if composer_id in storage.composer_data_ids
            else {}
        )
        repo, _ = resolve_repo_from_fallback(meta, resolver) if meta else ("(unknown)", "low")
        if repo == "(unknown)":
            continue
        attributed += 1
        attributed_bytes += nbytes
        if meta.get("_inferred_from_fsPath"):
            via_fs_path += 1
        elif meta.get("workspaceIdentifier"):
            via_workspace += 1
        elif meta.get("trackedGitRepos"):
            via_repo_path += 1

    ratio = attributed_bytes / total_bytes if total_bytes else 0.0
    return FallbackCoverageReport(
        orphan_sessions=total,
        orphan_bytes=total_bytes,
        with_composer_data=with_composer_data,
        attributed_sessions=attributed,
        attributed_bytes=attributed_bytes,
        coverage_ratio=ratio,
        via_repo_path=via_repo_path,
        via_workspace=via_workspace,
        via_fs_path=via_fs_path,
    )


def print_fallback_coverage_report(report: FallbackCoverageReport) -> None:
    print("CursorScope orphan repo fallback coverage")
    print(f"orphan sessions with storage: {report.orphan_sessions:,}")
    print(f"orphan bytes:                 {human_bytes(report.orphan_bytes)}")
    print(f"with composerData extracted:  {report.with_composer_data:,}")
    print(f"repo attributed (medium):     {report.attributed_sessions:,}")
    print(f"attributed bytes:             {human_bytes(report.attributed_bytes)}")
    print(f"coverage:                     {report.coverage_ratio * 100:.1f}%")
    print(f"via trackedGitRepos/repoPath: {report.via_repo_path:,}")
    print(f"via workspaceIdentifier:      {report.via_workspace:,}")
    print(f"via fsPath inference:         {report.via_fs_path:,}")
