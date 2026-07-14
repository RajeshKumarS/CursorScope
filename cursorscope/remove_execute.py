"""Destructive repo cleanup — ops only; not part of the read-only CLI surface."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analyze import load_headers
from .db import snapshot_copy
from .remove_plan import RemovePlan, exclusive_agentkv_hashes, session_key_patterns
from .scan import cleanup_scan_cache


@dataclass
class PruneResult:
    source: str
    backup_dir: str
    audit_log: str
    sessions_removed: int
    kv_rows_deleted: int
    header_entries_removed: int
    agentkv_blobs_deleted: int
    agentkv_bytes_deleted: int
    vacuum_ran: bool
    old_backups_removed: int = 0
    warnings: list[str] = field(default_factory=list)


def default_backup_dir() -> Path:
    return Path.home() / ".cursorscope" / "backups"


def default_audit_dir() -> Path:
    return Path.home() / ".cursorscope" / "history"


def prune_backup_dirs(backup_dir: Path | None = None, *, keep: int = 0) -> list[Path]:
    """Remove old prune backups, keeping the newest ``keep`` directories."""
    backup_dir = (backup_dir or default_backup_dir()).resolve()
    if not backup_dir.is_dir():
        return []
    dirs = sorted(
        (path for path in backup_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )
    removed: list[Path] = []
    for path in dirs[: max(0, len(dirs) - keep)]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return removed


def backup_dir_size(backup_dir: Path | None = None) -> int:
    backup_dir = (backup_dir or default_backup_dir()).resolve()
    if not backup_dir.is_dir():
        return 0
    return sum(path.stat().st_size for path in backup_dir.rglob("*") if path.is_file())


def cursor_is_running() -> bool:
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Cursor.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return "Cursor.exe" in result.stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
    for command in (["pgrep", "-x", "Cursor"], ["pgrep", "-f", "Cursor"]):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def backup_database(source: Path, backup_dir: Path | None = None) -> Path:
    backup_dir = (backup_dir or default_backup_dir()).resolve()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest_dir = backup_dir / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    snapshot_copy(source, dest_dir / "state.vscdb", source=source)
    return dest_dir


def open_rw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _remove_header_entries(conn: sqlite3.Connection, composer_ids: set[str]) -> int:
    row = conn.execute(
        "SELECT value FROM ItemTable WHERE key = ?",
        ("composer.composerHeaders",),
    ).fetchone()
    if not row:
        return 0
    raw = row[0]
    text = raw if isinstance(raw, str) else raw.decode("utf-8")
    obj = json.loads(text)
    before = len(obj.get("allComposers") or [])
    obj["allComposers"] = [
        entry
        for entry in (obj.get("allComposers") or [])
        if entry.get("composerId") not in composer_ids
    ]
    after = len(obj["allComposers"])
    if after != before:
        conn.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (json.dumps(obj), "composer.composerHeaders"),
        )
    return before - after


def _delete_session_keys(conn: sqlite3.Connection, composer_id: str) -> int:
    deleted = 0
    for pattern in session_key_patterns(composer_id):
        if pattern.endswith("%"):
            cur = conn.execute("DELETE FROM cursorDiskKV WHERE key LIKE ?", (pattern,))
        else:
            cur = conn.execute("DELETE FROM cursorDiskKV WHERE key = ?", (pattern,))
        deleted += int(cur.rowcount or 0)
    return deleted


def _delete_agentkv_blobs(conn: sqlite3.Connection, hashes: set[str], blobs: dict[str, int]) -> tuple[int, int]:
    deleted_rows = 0
    deleted_bytes = 0
    for digest in sorted(hashes):
        key = f"agentKv:blob:{digest}"
        cur = conn.execute("DELETE FROM cursorDiskKV WHERE key = ?", (key,))
        if int(cur.rowcount or 0):
            deleted_rows += 1
            deleted_bytes += blobs.get(digest, 0)
    return deleted_rows, deleted_bytes


def execute_remove_plan(
    plan: RemovePlan,
    *,
    source: Path,
    backup_dir: Path | None = None,
    audit_dir: Path | None = None,
    gc_blobs: bool = True,
    vacuum: bool = False,
    require_cursor_closed: bool = True,
    keep_backups: int = 1,
) -> PruneResult:
    if not plan.sessions:
        raise ValueError("remove plan has no sessions")

    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")

    if require_cursor_closed and cursor_is_running():
        raise RuntimeError(
            "Cursor appears to be running. Quit Cursor completely before pruning state.vscdb."
        )

    composer_ids = {session.composer_id for session in plan.sessions}
    backup_path = backup_database(source, backup_dir)

    kv_deleted = 0
    header_removed = 0
    blob_rows = 0
    blob_bytes = 0
    warnings: list[str] = []

    conn = open_rw(source)
    try:
        conn.execute("BEGIN IMMEDIATE")
        exclusive_hashes, blobs = exclusive_agentkv_hashes(conn, composer_ids) if gc_blobs else (set(), {})

        for composer_id in sorted(composer_ids):
            kv_deleted += _delete_session_keys(conn, composer_id)

        header_removed = _remove_header_entries(conn, composer_ids)

        if gc_blobs and exclusive_hashes:
            blob_rows, blob_bytes = _delete_agentkv_blobs(conn, exclusive_hashes, blobs)

        conn.commit()

        if vacuum:
            conn.execute("VACUUM")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    cleanup_scan_cache()

    audit_dir = (audit_dir or default_audit_dir()).resolve()
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    audit_path = audit_dir / f"prune-{stamp}.json"
    result = PruneResult(
        source=str(source),
        backup_dir=str(backup_path),
        audit_log=str(audit_path),
        sessions_removed=len(plan.sessions),
        kv_rows_deleted=kv_deleted,
        header_entries_removed=header_removed,
        agentkv_blobs_deleted=blob_rows,
        agentkv_bytes_deleted=blob_bytes,
        vacuum_ran=vacuum,
        warnings=warnings,
    )
    audit_path.write_text(
        json.dumps(
            {
                "result": asdict(result),
                "plan": {
                    "repos_requested": plan.repos_requested,
                    "repos_matched": plan.repos_matched,
                    "session_count": plan.session_count,
                    "attributed_bytes": plan.attributed_bytes,
                    "composer_ids": sorted(composer_ids),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    old_backups_removed = prune_backup_dirs(backup_dir, keep=max(0, keep_backups))
    result.old_backups_removed = len(old_backups_removed)
    return result
