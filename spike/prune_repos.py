#!/usr/bin/env python3
"""Experimental destructive repo cleanup — ops only, not part of supported CursorScope CLI.

Default is dry-run. Use --execute only after reviewing `cursorscope plan-remove`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cursorscope.analyze import human_bytes
from cursorscope.db import default_source_db
from cursorscope.remove_execute import PruneResult, backup_dir_size, cursor_is_running, execute_remove_plan
from cursorscope.remove_plan import build_remove_plan, print_remove_plan
from cursorscope.workspace import WorkspaceResolver, default_workspace_storage_dir


def print_banner() -> None:
    print("=" * 72)
    print("CursorScope experimental prune (DESTRUCTIVE)")
    print("This mutates live state.vscdb. Main CursorScope remains read-only.")
    print("=" * 72)
    print()


def print_prune_result(result: PruneResult) -> None:
    print("Prune complete")
    print(f"source:              {result.source}")
    print(f"backup:              {result.backup_dir}")
    print(f"audit log:           {result.audit_log}")
    print(f"sessions removed:    {result.sessions_removed:,}")
    print(f"KV rows deleted:     {result.kv_rows_deleted:,}")
    print(f"header entries removed: {result.header_entries_removed:,}")
    print(
        f"agentKv blobs deleted: {result.agentkv_blobs_deleted:,} "
        f"({human_bytes(result.agentkv_bytes_deleted)})"
    )
    print(f"VACUUM ran:          {'yes' if result.vacuum_ran else 'no'}")
    if result.old_backups_removed:
        print(f"old prune backups removed: {result.old_backups_removed}")
    print()
    backup_total = backup_dir_size(Path(result.backup_dir).parent)
    if backup_total:
        print(
            f"Prune backups on disk: {human_bytes(backup_total)} under "
            f"{Path(result.backup_dir).parent}"
        )
        print("  One backup is kept for rollback. Delete all with: cursorscope cleanup --backups")
        print()
    print("Next steps:")
    print("  1. Restart Cursor")
    print("  2. Run Developer: GC Agent KV Blobs")
    print("  3. cursorscope checkpoint create \"after repo cleanup\"")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prune_repos.py",
        description="Experimental repo cleanup for live state.vscdb (spike/ops only)",
    )
    parser.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Repository label from `cursorscope analyze` (repeatable)",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Live global state.vscdb (default: %%APPDATA%%/Cursor/User/globalStorage/state.vscdb)",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=None,
        help="Cursor workspaceStorage directory",
    )
    parser.add_argument(
        "--match",
        choices=("exact", "suffix"),
        default="exact",
        help="Match repo labels exactly or by path suffix",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply deletion after safety checks (default: dry-run only)",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="Required with --execute: pass DELETE to authorize",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Backup directory (default: ~/.cursorscope/backups)",
    )
    parser.add_argument(
        "--keep-backups",
        type=int,
        default=1,
        help="After --execute, keep only the newest N prune backups (default: 1)",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Run VACUUM after deletion (slow on multi-GB databases)",
    )
    parser.add_argument(
        "--no-gc-blobs",
        action="store_true",
        help="Do not delete agentKv blobs exclusively referenced by removed sessions",
    )
    parser.add_argument(
        "--allow-cursor-running",
        action="store_true",
        help="Skip Cursor process check (unsafe)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not reuse cached cursorDiskKV scan results while planning",
    )
    parser.add_argument(
        "--skip-agentkv",
        action="store_true",
        help="Skip agentKv reclaim estimates during planning",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print_banner()

    source = (args.src or default_source_db()).resolve()
    if not source.is_file():
        print(f"Database not found: {source}", file=sys.stderr)
        return 1

    resolver = WorkspaceResolver(args.workspace_dir or default_workspace_storage_dir())
    plan = build_remove_plan(
        source,
        source=source,
        repos=args.repo,
        resolver=resolver,
        match_mode=args.match,
        use_cache=not args.no_cache,
        scan_agentkv=not args.skip_agentkv,
    )
    print_remove_plan(plan)
    print()

    if not plan.sessions:
        return 1

    if not args.execute:
        print("Dry-run only. Re-run with --execute --confirm DELETE to apply.")
        return 0

    if args.confirm != "DELETE":
        print("Refusing --execute without --confirm DELETE", file=sys.stderr)
        return 1

    if cursor_is_running() and not args.allow_cursor_running:
        print(
            "Cursor appears to be running. Quit Cursor completely, then retry.",
            file=sys.stderr,
        )
        return 1

    print("Applying prune to live database...")
    print(f"  source: {source}")
    print()
    try:
        result = execute_remove_plan(
            plan,
            source=source,
            backup_dir=args.backup_dir,
            gc_blobs=not args.no_gc_blobs,
            vacuum=args.vacuum,
            require_cursor_closed=not args.allow_cursor_running,
            keep_backups=max(0, args.keep_backups),
        )
    except Exception as exc:
        print(f"Prune failed: {exc}", file=sys.stderr)
        return 1

    print_prune_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
