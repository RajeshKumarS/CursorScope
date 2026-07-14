from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import __version__
from .analyze import AnalysisReport, build_report, human_bytes
from .db import (
    ResolvedDb,
    SnapshotResult,
    cleanup_snapshot_dir,
    default_snapshot_path,
    default_source_db,
    find_reusable_snapshot,
    open_ro,
    prune_old_snapshots,
    release_resolved_db,
    snapshot_copy,
)
from .diagnostics import build_fallback_coverage_report, print_fallback_coverage_report
from .blob_probe import build_blob_probe_bundle, blob_probe_to_dict, print_blob_probe_report
from .scan import cleanup_scan_cache
from .integrity import (
    build_integrity_report,
    integrity_report_to_dict,
    print_integrity_report,
)
from .session import build_session_report, print_session_report, session_report_to_dict
from .sessions import build_sessions_report, print_sessions_report, sessions_report_to_dict
from .timeline import build_timeline_report, print_timeline_report, timeline_report_to_dict
from .history import (
    build_growth_report,
    create_checkpoint,
    default_history_dir,
    delete_checkpoint,
    diff_checkpoints,
    growth_to_dict,
    list_checkpoints,
    list_scans,
    print_checkpoint_list,
    print_growth_report,
    print_history_list,
    record_to_dict,
)
from .remove_plan import build_remove_plan, plan_to_dict, print_remove_plan
from .workspace import WorkspaceResolver, default_workspace_storage_dir


def _report_to_dict(report: AnalysisReport) -> dict:
    from dataclasses import asdict

    data = asdict(report)
    data["pages"] = asdict(report.pages)
    if report.agentkv:
        data["agentkv"] = asdict(report.agentkv)
    return data


def print_report(report: AnalysisReport) -> None:
    pages = report.pages
    print("CursorScope")
    print(f"source:   {report.source}")
    print(f"snapshot: {report.snapshot}")
    print(f"health:   quick_check={report.quick_check}")
    print(
        f"pages:    {human_bytes(pages.file_bytes)} total, "
        f"{human_bytes(pages.reclaimable_bytes)} reclaimable ({pages.reclaimable_ratio * 100:.1f}%)"
    )
    print(f"rows:     ItemTable={report.item_table_rows:,}  cursorDiskKV={report.kv_rows:,}")
    print()

    print("Storage by object type")
    print(f"{'Type':<42} {'Count':>8}  {'Size':>10}")
    print("-" * 64)
    for row in report.storage_by_type:
        print(f"{row.prefix:<42} {row.count:>8,}  {human_bytes(row.bytes):>10}")
    print()

    print("Top repositories (header-linked sessions only)")
    print(f"{'Size':>10}  {'Sessions':>8}  {'Confidence':<10}  Repository")
    print("-" * 80)
    for row in report.top_repos:
        print(
            f"{human_bytes(row.bytes):>10}  {row.session_count:>8}  "
            f"{row.confidence:<10}  {row.repo}"
        )
    print()

    print("Top Composer sessions")
    print(f"{'Size':>10}  {'Conf':<6}  {'Name / subtitle'}")
    print("-" * 80)
    for row in report.top_sessions:
        label = row.name or row.subtitle or row.composer_id
        if row.name and row.subtitle:
            label = f"{row.name} - {row.subtitle}"
        if row.repo:
            label = f"{label} [{row.repo}]"
        print(f"{human_bytes(row.bytes):>10}  {row.confidence:<6}  {label}")
        if row.breakdown:
            parts = ", ".join(
                f"{k}={human_bytes(v)}" for k, v in sorted(row.breakdown.items(), key=lambda x: -x[1])
            )
            print(f"{'':>10}           {parts}")
    print()

    print("Unattributed / low-confidence storage")
    for key, size in report.unattributed.items():
        print(f"  {key:<28} {human_bytes(size):>10}")
    print()

    if report.agentkv:
        akv = report.agentkv
        print("AgentKv blob attribution")
        print(f"  total blobs:              {akv.total_blobs:,}")
        print(f"  total bytes:              {human_bytes(akv.total_bytes)}")
        print(f"  referenced blobs:         {akv.referenced_blobs:,} ({human_bytes(akv.referenced_bytes_unique)})")
        print(f"  unreferenced blobs:       {akv.unreferenced_blobs:,} ({human_bytes(akv.unreferenced_bytes)})")
        print(
            f"  composer.content store:   {akv.composer_content_blobs:,} blobs "
            f"({human_bytes(akv.composer_content_bytes)}; {akv.overlap_blobs} overlap agentKv)"
        )
        print(f"  structural ref rows:      {akv.structural_reference_rows:,}")
        for note in akv.notes:
            print(f"  Note: {note}")
        print()

    print("Integrity signals")
    for key, value in report.integrity.items():
        print(f"  {key}: {value}")


def add_db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Path to live state.vscdb (default: %%APPDATA%%/Cursor/User/globalStorage/state.vscdb)",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Analyze an existing snapshot file (no copy created)",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=None,
        help="Directory for cached snapshot copies (default: ~/.cursorscope/snapshots)",
    )
    snapshot_mode = parser.add_mutually_exclusive_group()
    snapshot_mode.add_argument(
        "--cached-snapshot",
        action="store_true",
        help="Copy state.vscdb to ~/.cursorscope and reuse until the live DB changes",
    )
    snapshot_mode.add_argument(
        "--ephemeral",
        action="store_true",
        help="Copy to temp storage for this run only (deleted on exit)",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="With --cached-snapshot, force a new copy even if a reusable one exists",
    )
    parser.add_argument(
        "--discard-snapshot",
        action="store_true",
        help="With --cached-snapshot, delete the cached copy after the command finishes",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=None,
        help="Cursor workspaceStorage directory (default: %%APPDATA%%/Cursor/User/workspaceStorage)",
    )
    parser.add_argument(
        "--skip-agentkv",
        action="store_true",
        help="Skip agentKv blob attribution scan (faster)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip agentKv scan; integrity also omits unknown-prefix detail table",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not reuse cached cursorDiskKV scan results",
    )


def workspace_resolver(args: argparse.Namespace) -> WorkspaceResolver:
    storage_dir = args.workspace_dir or default_workspace_storage_dir()
    return WorkspaceResolver(storage_dir)


def warn_snapshot_disk_cost(source: Path, *, json_output: bool) -> None:
    if json_output or not source.is_file():
        return
    size = source.stat().st_size
    wal = Path(str(source) + "-wal")
    if wal.is_file():
        size += wal.stat().st_size
    print(
        f"Warning: snapshot needs ~{human_bytes(size)} free disk space.",
        file=sys.stderr,
    )


def resolve_live_db(source: Path, *, json_output: bool) -> ResolvedDb | None:
    if not source.exists():
        print(f"Source DB not found: {source}", file=sys.stderr)
        return None
    if not json_output:
        print(
            "Reading live state.vscdb read-only (no disk copy).",
            file=sys.stderr,
        )
        print(
            "Use --cached-snapshot or --ephemeral only if you need a point-in-time copy "
            "and have free disk space.",
            file=sys.stderr,
        )
    return ResolvedDb(db_path=source, source=source)


def resolve_db_path(args: argparse.Namespace, *, json_output: bool) -> ResolvedDb | None:
    source = (args.src or default_source_db()).resolve()
    discard_after = getattr(args, "discard_snapshot", False)

    if args.snapshot:
        db_path = args.snapshot.resolve()
        if not db_path.exists():
            print(f"Snapshot not found: {db_path}", file=sys.stderr)
            return None
        return ResolvedDb(db_path=db_path, source=source)

    if not args.cached_snapshot and not args.ephemeral:
        return resolve_live_db(source, json_output=json_output)

    if not source.exists():
        print(f"Source DB not found: {source}", file=sys.stderr)
        return None

    warn_snapshot_disk_cost(source, json_output=json_output)
    snapshot_dir = (args.snapshot_dir or Path.home() / ".cursorscope" / "snapshots").resolve()

    if args.ephemeral:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="cursorscope-", dir=snapshot_dir))
        db_path = temp_dir / "state.vscdb"
        if not json_output:
            print(f"Snapshotting {source} (ephemeral) ...", file=sys.stderr)
        result: SnapshotResult = snapshot_copy(source, db_path)
        if not json_output:
            print(
                f"Ephemeral snapshot ready ({human_bytes(sum(n for _, n in result.files))}, "
                f"{result.elapsed_sec:.1f}s); will be deleted on exit",
                file=sys.stderr,
            )
        return ResolvedDb(
            db_path=db_path,
            source=source,
            ephemeral=True,
            temp_dir=temp_dir,
        )

    if not args.fresh:
        reusable = find_reusable_snapshot(snapshot_dir, source)
        if reusable is not None:
            if not json_output:
                print(f"Reusing snapshot: {reusable}", file=sys.stderr)
            return ResolvedDb(
                db_path=reusable,
                source=source,
                discard_after=discard_after,
            )

    db_path = default_snapshot_path(snapshot_dir)
    if not json_output:
        print(f"Snapshotting {source} ...", file=sys.stderr)
    result = snapshot_copy(source, db_path, source=source)
    prune_old_snapshots(snapshot_dir, keep=db_path)
    if not json_output:
        print(
            f"Snapshot ready ({human_bytes(sum(n for _, n in result.files))}, "
            f"{result.elapsed_sec:.1f}s): {db_path}",
            file=sys.stderr,
        )
    return ResolvedDb(
        db_path=db_path,
        source=source,
        discard_after=discard_after,
    )


def with_resolved_db(
    args: argparse.Namespace,
    *,
    json_output: bool,
    run,
) -> int:
    resolved = resolve_db_path(args, json_output=json_output)
    if resolved is None:
        return 1
    try:
        return run(resolved)
    finally:
        release_resolved_db(resolved)


def build_analyze_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope", add_help=False)
    add_db_args(parser)
    parser.add_argument("--top", type=int, default=15, help="Number of top sessions/repos to show")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not save scan summary to ~/.cursorscope/history/",
    )
    return parser


def build_sessions_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope sessions", add_help=False)
    add_db_args(parser)
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="Only list sessions with storage but no composer.composerHeaders entry",
    )
    parser.add_argument("--top", type=int, default=20, help="Number of sessions to show")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def build_integrity_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope integrity", add_help=False)
    add_db_args(parser)
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Number of top orphan sessions / gaps to show",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def build_coverage_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope coverage", add_help=False)
    add_db_args(parser)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def build_probe_blobs_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope probe-blobs", add_help=False)
    add_db_args(parser)
    parser.add_argument("--sample", type=int, default=500)
    parser.add_argument("--content-sample", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    return parser


def build_history_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope history", add_help=False)
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Filter to scans of a specific state.vscdb path",
    )
    parser.add_argument("--limit", type=int, default=20, help="Number of scans to list")
    parser.add_argument("--json", action="store_true")
    return parser


def build_growth_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope growth", add_help=False)
    add_db_args(parser)
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def build_timeline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope timeline", add_help=False)
    add_db_args(parser)
    parser.add_argument("--repo", default=None, help="Filter to one repository label")
    parser.add_argument("--limit", type=int, default=500, help="Max sessions in list output")
    parser.add_argument("--json", action="store_true")
    return parser


def build_session_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope session", add_help=False)
    add_db_args(parser)
    parser.add_argument("composer_id", help="Composer session UUID (full or unique prefix)")
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of largest bubbles/checkpoints to show",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def build_plan_remove_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursorscope plan-remove", add_help=False)
    add_db_args(parser)
    parser.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Repository label from analyze output (repeatable)",
    )
    parser.add_argument(
        "--match",
        choices=("exact", "suffix"),
        default="exact",
        help="Match repo labels exactly or by path suffix (default: exact)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def build_root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cursorscope",
        description="Read-only analysis of Cursor globalStorage state.vscdb",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    analyze = subparsers.add_parser("analyze", help="Database-wide storage overview")
    add_db_args(analyze)
    analyze.add_argument("--top", type=int, default=15)
    analyze.add_argument("--json", action="store_true")
    analyze.add_argument("--no-history", action="store_true")
    analyze.set_defaults(func="analyze")

    session = subparsers.add_parser("session", help="Drill into one Composer session")
    add_db_args(session)
    session.add_argument("composer_id")
    session.add_argument("--top", type=int, default=20)
    session.add_argument("--json", action="store_true")
    session.set_defaults(func="session")

    integrity = subparsers.add_parser("integrity", help="Database health and orphan diagnostics")
    add_db_args(integrity)
    integrity.add_argument("--top", type=int, default=15)
    integrity.add_argument("--json", action="store_true")
    integrity.set_defaults(func="integrity")

    sessions = subparsers.add_parser("sessions", help="List Composer sessions by storage")
    add_db_args(sessions)
    sessions.add_argument("--no-header", action="store_true")
    sessions.add_argument("--top", type=int, default=20)
    sessions.add_argument("--json", action="store_true")
    sessions.set_defaults(func="sessions")

    cleanup = subparsers.add_parser("cleanup", help="Delete cached snapshot files")
    cleanup.add_argument(
        "--snapshot-dir",
        type=Path,
        default=None,
        help="Snapshot directory to clean (default: ~/.cursorscope/snapshots)",
    )
    cleanup.add_argument(
        "--backups",
        action="store_true",
        help="Also remove prune backups from ~/.cursorscope/backups/",
    )
    cleanup.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Prune backup directory (default: ~/.cursorscope/backups)",
    )
    cleanup.add_argument(
        "--keep-backups",
        type=int,
        default=0,
        help="With --backups, keep the newest N backup folders (default: 0)",
    )
    cleanup.set_defaults(func="cleanup")

    coverage = subparsers.add_parser(
        "coverage",
        help="Orphan session composerData repo fallback coverage",
    )
    add_db_args(coverage)
    coverage.add_argument("--json", action="store_true")
    coverage.set_defaults(func="coverage")

    probe = subparsers.add_parser(
        "probe-blobs",
        help="Fingerprint internal formats of agentKv and composer.content blobs",
    )
    add_db_args(probe)
    probe.add_argument("--sample", type=int, default=500, help="agentKv blobs to sample (largest first)")
    probe.add_argument("--content-sample", type=int, default=200)
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(func="probe-blobs")

    timeline = subparsers.add_parser(
        "timeline",
        help="Session timeline from composer metadata (Phase A)",
    )
    add_db_args(timeline)
    timeline.add_argument("--repo", default=None)
    timeline.add_argument("--limit", type=int, default=500)
    timeline.add_argument("--json", action="store_true")
    timeline.set_defaults(func="timeline")

    history = subparsers.add_parser(
        "history",
        help="List saved scan summaries (~/.cursorscope/history/)",
    )
    history.add_argument("--src", type=Path, default=None)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--json", action="store_true")
    history.set_defaults(func="history")

    growth = subparsers.add_parser(
        "growth",
        help="Compare current database vs last scan and Cursor backup",
    )
    add_db_args(growth)
    growth.add_argument("--no-history", action="store_true")
    growth.add_argument("--json", action="store_true")
    growth.set_defaults(func="growth")

    plan_remove = subparsers.add_parser(
        "plan-remove",
        help="Preview repository/session removal (read-only; no writes)",
    )
    add_db_args(plan_remove)
    plan_remove.add_argument("--repo", action="append", required=True)
    plan_remove.add_argument("--match", choices=("exact", "suffix"), default="exact")
    plan_remove.add_argument("--json", action="store_true")
    plan_remove.set_defaults(func="plan-remove")

    checkpoint = subparsers.add_parser(
        "checkpoint",
        help="Named storage summary checkpoints (no DB copy)",
    )
    cp_sub = checkpoint.add_subparsers(dest="checkpoint_cmd")

    cp_create = cp_sub.add_parser("create", help="Save a labeled summary of current storage")
    add_db_args(cp_create)
    cp_create.add_argument("label", help="Checkpoint name, e.g. 'before cleanup'")
    cp_create.add_argument("--note", default=None, help="Optional note")
    cp_create.add_argument("--json", action="store_true")
    cp_create.set_defaults(func="checkpoint-create")

    cp_list = cp_sub.add_parser("list", help="List saved checkpoints")
    cp_list.add_argument("--src", type=Path, default=None)
    cp_list.add_argument("--limit", type=int, default=50)
    cp_list.add_argument("--json", action="store_true")
    cp_list.set_defaults(func="checkpoint-list")

    cp_diff = cp_sub.add_parser("diff", help="Compare two checkpoints")
    cp_diff.add_argument("from_ref", help="Earlier checkpoint (label or id)")
    cp_diff.add_argument("to_ref", help="Later checkpoint (label or id)")
    cp_diff.add_argument("--src", type=Path, default=None)
    cp_diff.add_argument("--json", action="store_true")
    cp_diff.set_defaults(func="checkpoint-diff")

    cp_delete = cp_sub.add_parser("delete", help="Delete a checkpoint")
    cp_delete.add_argument("ref", help="Checkpoint label or id")
    cp_delete.add_argument("--src", type=Path, default=None)
    cp_delete.add_argument("--json", action="store_true")
    cp_delete.set_defaults(func="checkpoint-delete")

    serve = subparsers.add_parser("serve", help="Run local HTTP API + desktop UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--open",
        action="store_true",
        help="Open browser after the server starts",
    )
    serve.set_defaults(func="serve")

    return parser


def cmd_cleanup(args: argparse.Namespace) -> int:
    from .history import default_history_dir, prune_old_scans
    from .remove_execute import default_backup_dir, prune_backup_dirs

    snapshot_dir = (args.snapshot_dir or Path.home() / ".cursorscope" / "snapshots").resolve()
    removed = cleanup_snapshot_dir(snapshot_dir)
    cache_removed = cleanup_scan_cache()
    history_removed = prune_old_scans(history_dir=default_history_dir())
    backup_removed: list[Path] = []
    if getattr(args, "backups", False):
        backup_removed = prune_backup_dirs(
            args.backup_dir or default_backup_dir(),
            keep=max(0, int(getattr(args, "keep_backups", 0))),
        )
    total = len(removed) + len(cache_removed) + len(history_removed) + len(backup_removed)
    if total:
        parts = [
            f"{len(removed)} snapshot file(s)",
            f"{len(cache_removed)} scan cache file(s)",
            f"{len(history_removed)} old history file(s)",
        ]
        if backup_removed:
            parts.append(f"{len(backup_removed)} prune backup folder(s)")
        print(f"Removed {', '.join(parts)}")
    else:
        print("No snapshot, scan cache, history, or backup files to remove")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        report = build_report(
            resolved.db_path,
            source=resolved.source,
            top_n=args.top,
            resolver=workspace_resolver(args),
            scan_agentkv=not args.skip_agentkv,
            progress=not args.json,
            use_cache=not args.no_cache,
            fast=args.fast,
        )
        growth = build_growth_report(
            report,
            save=not args.no_history,
            resolver=workspace_resolver(args),
        )
        if args.json:
            payload = _report_to_dict(report)
            payload["growth"] = growth_to_dict(growth)
            print(json.dumps(payload, indent=2))
        else:
            print_report(report)
            if not args.no_history:
                print()
                print_growth_report(growth)
        return 0

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_sessions(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        report = build_sessions_report(
            resolved.db_path,
            source=resolved.source,
            top_n=args.top,
            no_header=args.no_header,
            resolver=workspace_resolver(args),
        )
        if args.json:
            print(json.dumps(sessions_report_to_dict(report), indent=2))
        else:
            print_sessions_report(report)
        return 0

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_integrity(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        report = build_integrity_report(
            resolved.db_path,
            source=resolved.source,
            top_n=args.top,
            resolver=workspace_resolver(args),
            scan_agentkv=not args.skip_agentkv,
            progress=not args.json,
            use_cache=not args.no_cache,
            fast=args.fast,
        )
        if args.json:
            print(json.dumps(integrity_report_to_dict(report), indent=2))
        else:
            print_integrity_report(report)
        return 0 if report.quick_check == "ok" else 1

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_session(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        report = build_session_report(
            resolved.db_path,
            args.composer_id,
            source=resolved.source,
            top_n=args.top,
            resolver=workspace_resolver(args),
            scan_agentkv=not args.skip_agentkv,
        )
        if args.json:
            print(json.dumps(session_report_to_dict(report), indent=2))
        else:
            print_session_report(report)
        return 0 if report.found else 1

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_coverage(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        conn = open_ro(resolved.db_path)
        try:
            report = build_fallback_coverage_report(
                conn,
                resolved.db_path,
                resolver=workspace_resolver(args),
                use_cache=not args.no_cache,
            )
        finally:
            conn.close()
        if args.json:
            from dataclasses import asdict

            print(json.dumps(asdict(report), indent=2))
        else:
            print_fallback_coverage_report(report)
        return 0

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_timeline(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        report = build_timeline_report(
            resolved.db_path,
            source=resolved.source,
            resolver=workspace_resolver(args),
            repo_filter=args.repo,
            limit=args.limit,
            use_cache=not args.no_cache,
        )
        if args.json:
            print(json.dumps(timeline_report_to_dict(report), indent=2))
        else:
            print_timeline_report(report)
        return 0

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_history(args: argparse.Namespace) -> int:
    source = args.src or default_source_db()
    records = list_scans(source=source, limit=args.limit)
    if args.json:
        print(
            json.dumps(
                {
                    "source": str(source.resolve()),
                    "scans": [record_to_dict(record) for record in records],
                },
                indent=2,
            )
        )
    else:
        print_history_list(records)
    return 0


def cmd_growth(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        report = build_report(
            resolved.db_path,
            source=resolved.source,
            top_n=15,
            resolver=workspace_resolver(args),
            scan_agentkv=not args.skip_agentkv,
            progress=not args.json,
            use_cache=not args.no_cache,
            fast=args.fast,
        )
        growth = build_growth_report(
            report,
            save=not args.no_history,
            resolver=workspace_resolver(args),
        )
        if args.json:
            print(json.dumps(growth_to_dict(growth), indent=2))
        else:
            print("CursorScope growth (Phase B)")
            print(f"source: {report.source}")
            print()
            print_growth_report(growth)
        return 0

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_checkpoint_create(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        report = build_report(
            resolved.db_path,
            source=resolved.source,
            top_n=15,
            top_n_repos=25,
            resolver=workspace_resolver(args),
            scan_agentkv=not args.skip_agentkv,
            progress=not args.json,
            use_cache=not args.no_cache,
            fast=args.fast,
        )
        record = create_checkpoint(report, args.label, note=args.note)
        if args.json:
            print(json.dumps(record_to_dict(record), indent=2))
        else:
            print(f"Checkpoint saved: {record.label}")
            print(f"  id:       {record.id}")
            print(f"  recorded: {record.recorded_at} UTC")
            print(f"  file:     {human_bytes(record.database_file_bytes)}")
            print(f"  path:     {default_history_dir() / (record.id + '.json')}")
        return 0

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_checkpoint_list(args: argparse.Namespace) -> int:
    source = args.src or default_source_db()
    records = list_checkpoints(source=source, limit=args.limit)
    if args.json:
        print(
            json.dumps(
                {
                    "source": str(source.resolve()),
                    "checkpoints": [record_to_dict(record) for record in records],
                },
                indent=2,
            )
        )
    else:
        print_checkpoint_list(records)
        if records:
            print(f"History directory: {default_history_dir()}")
    return 0


def cmd_checkpoint_diff(args: argparse.Namespace) -> int:
    source = args.src or default_source_db()
    try:
        growth = diff_checkpoints(args.from_ref, args.to_ref, source=source)
    except ValueError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(growth_to_dict(growth), indent=2))
    else:
        print("CursorScope checkpoint diff")
        print(f"source: {source}")
        print()
        print_growth_report(growth)
    return 0


def cmd_checkpoint_delete(args: argparse.Namespace) -> int:
    source = args.src or default_source_db()
    try:
        deleted = delete_checkpoint(args.ref, source=source)
    except ValueError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    payload = {"deleted": deleted, "ref": args.ref}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Deleted checkpoint: {args.ref}" if deleted else f"Not found: {args.ref}")
    return 0 if deleted else 1


def cmd_probe_blobs(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        conn = open_ro(resolved.db_path)
        try:
            bundle = build_blob_probe_bundle(
                conn,
                agentkv_sample=args.sample,
                content_sample=args.content_sample,
            )
        finally:
            conn.close()
        if args.json:
            print(json.dumps(blob_probe_to_dict(bundle), indent=2))
        else:
            print_blob_probe_report(bundle)
        return 0

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_plan_remove(args: argparse.Namespace) -> int:
    def run(resolved: ResolvedDb) -> int:
        plan = build_remove_plan(
            resolved.db_path,
            source=resolved.source,
            repos=args.repo,
            resolver=workspace_resolver(args),
            match_mode=args.match,
            use_cache=not args.no_cache,
            scan_agentkv=not args.skip_agentkv,
        )
        if args.json:
            print(json.dumps(plan_to_dict(plan), indent=2))
        else:
            print_remove_plan(plan)
        return 0 if plan.sessions else 1

    return with_resolved_db(args, json_output=args.json, run=run)


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import run_server

    run_server(host=args.host, port=args.port, auto_open=args.open)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    if not argv or argv[0] not in {
        "analyze",
        "session",
        "sessions",
        "integrity",
        "coverage",
        "cleanup",
        "serve",
        "probe-blobs",
        "timeline",
        "history",
        "growth",
        "plan-remove",
        "checkpoint",
        "-h",
        "--help",
        "--version",
    }:
        args = build_analyze_parser().parse_args(argv)
        return cmd_analyze(args)

    if argv[0] == "cleanup":
        parser = argparse.ArgumentParser(prog="cursorscope cleanup")
        parser.add_argument(
            "--snapshot-dir",
            type=Path,
            default=None,
            help="Snapshot directory to clean (default: ~/.cursorscope/snapshots)",
        )
        parser.add_argument(
            "--backups",
            action="store_true",
            help="Also remove prune backups from ~/.cursorscope/backups/",
        )
        parser.add_argument(
            "--backup-dir",
            type=Path,
            default=None,
            help="Prune backup directory (default: ~/.cursorscope/backups)",
        )
        parser.add_argument(
            "--keep-backups",
            type=int,
            default=0,
            help="With --backups, keep the newest N backup folders (default: 0)",
        )
        return cmd_cleanup(parser.parse_args(argv[1:]))

    if argv[0] == "sessions":
        args = build_sessions_parser().parse_args(argv[1:])
        return cmd_sessions(args)

    if argv[0] == "coverage":
        args = build_coverage_parser().parse_args(argv[1:])
        return cmd_coverage(args)

    if argv[0] == "probe-blobs":
        args = build_probe_blobs_parser().parse_args(argv[1:])
        return cmd_probe_blobs(args)

    if argv[0] == "timeline":
        args = build_timeline_parser().parse_args(argv[1:])
        return cmd_timeline(args)

    if argv[0] == "history":
        args = build_history_parser().parse_args(argv[1:])
        return cmd_history(args)

    if argv[0] == "growth":
        args = build_growth_parser().parse_args(argv[1:])
        return cmd_growth(args)

    if argv[0] == "plan-remove":
        args = build_plan_remove_parser().parse_args(argv[1:])
        return cmd_plan_remove(args)

    if argv[0] == "integrity":
        args = build_integrity_parser().parse_args(argv[1:])
        return cmd_integrity(args)

    if argv[0] == "session":
        args = build_session_parser().parse_args(argv[1:])
        return cmd_session(args)

    if argv[0] == "analyze":
        args = build_analyze_parser().parse_args(argv[1:])
        return cmd_analyze(args)

    if argv[0] == "serve":
        parser = argparse.ArgumentParser(prog="cursorscope serve")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8765)
        parser.add_argument("--open", action="store_true")
        return cmd_serve(parser.parse_args(argv[1:]))

    args = build_root_parser().parse_args(argv)
    if args.command == "cleanup":
        return cmd_cleanup(args)
    if args.command == "coverage":
        return cmd_coverage(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "probe-blobs":
        return cmd_probe_blobs(args)
    if args.command == "timeline":
        return cmd_timeline(args)
    if args.command == "history":
        return cmd_history(args)
    if args.command == "growth":
        return cmd_growth(args)
    if args.command == "plan-remove":
        return cmd_plan_remove(args)
    if args.command == "checkpoint":
        if args.checkpoint_cmd == "create":
            return cmd_checkpoint_create(args)
        if args.checkpoint_cmd == "list":
            return cmd_checkpoint_list(args)
        if args.checkpoint_cmd == "diff":
            return cmd_checkpoint_diff(args)
        if args.checkpoint_cmd == "delete":
            return cmd_checkpoint_delete(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
