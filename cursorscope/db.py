from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


def default_source_db() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def open_ro(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


@dataclass
class SnapshotResult:
    path: Path
    elapsed_sec: float
    files: list[tuple[Path, int]]
    reused: bool = False


@dataclass
class SourceFingerprint:
    size: int
    mtime_ns: int


def source_fingerprint(path: Path) -> SourceFingerprint:
    stat = path.stat()
    return SourceFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def default_snapshot_path(snapshot_dir: Path) -> Path:
    return snapshot_dir / "state-latest.vscdb"


def snapshot_manifest_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix(".json")


def read_snapshot_manifest(snapshot_path: Path) -> dict | None:
    manifest = snapshot_manifest_path(snapshot_path)
    if not manifest.is_file():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_snapshot_manifest(
    snapshot_path: Path,
    *,
    source: Path,
    fingerprint: SourceFingerprint,
) -> None:
    manifest = snapshot_manifest_path(snapshot_path)
    manifest.write_text(
        json.dumps(
            {
                "source": str(source.resolve()),
                "path": str(snapshot_path.resolve()),
                "size": fingerprint.size,
                "mtime_ns": fingerprint.mtime_ns,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def find_reusable_snapshot(snapshot_dir: Path, source: Path) -> Path | None:
    """Reuse the default snapshot when the live DB has not changed."""
    snapshot_path = default_snapshot_path(snapshot_dir)
    if not snapshot_path.is_file():
        return None

    manifest = read_snapshot_manifest(snapshot_path)
    if not manifest:
        return None

    fingerprint = source_fingerprint(source)
    if manifest.get("size") != fingerprint.size:
        return None
    if manifest.get("mtime_ns") != fingerprint.mtime_ns:
        return None
    if manifest.get("source") != str(source.resolve()):
        return None
    return snapshot_path


@dataclass
class ResolvedDb:
    db_path: Path
    source: Path
    ephemeral: bool = False
    temp_dir: Path | None = None
    discard_after: bool = False


def snapshot_sidecar_paths(snapshot_path: Path) -> list[Path]:
    paths = [snapshot_path, snapshot_manifest_path(snapshot_path)]
    for suffix in ("-wal", "-shm"):
        paths.append(Path(str(snapshot_path) + suffix))
    return paths


def delete_snapshot(snapshot_path: Path) -> list[Path]:
    """Delete a snapshot and its WAL/SHM/manifest sidecars."""
    removed: list[Path] = []
    for path in snapshot_sidecar_paths(snapshot_path):
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def cleanup_snapshot_dir(snapshot_dir: Path) -> list[Path]:
    """Remove all CursorScope snapshot files under snapshot_dir."""
    removed: list[Path] = []
    if not snapshot_dir.is_dir():
        return removed
    for path in list(snapshot_dir.iterdir()):
        name = path.name
        if path.is_file() and name.startswith("state-") and (
            name.endswith(".vscdb")
            or name.endswith(".vscdb-wal")
            or name.endswith(".vscdb-shm")
            or name.endswith(".json")
        ):
            path.unlink()
            removed.append(path)
        elif path.is_dir() and name.startswith("cursorscope-"):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
    return removed


def prune_old_snapshots(snapshot_dir: Path, *, keep: Path) -> list[Path]:
    """Remove other snapshot copies to avoid unbounded disk use."""
    removed: list[Path] = []
    keep = keep.resolve()
    keep_manifest = snapshot_manifest_path(keep).resolve()
    if not snapshot_dir.is_dir():
        return removed

    for path in snapshot_dir.iterdir():
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == keep or resolved == keep_manifest:
            continue
        name = path.name
        if name.startswith("state-") and (
            name.endswith(".vscdb")
            or name.endswith(".vscdb-wal")
            or name.endswith(".vscdb-shm")
            or name.endswith(".json")
        ):
            path.unlink()
            removed.append(path)
    return removed


def release_resolved_db(resolved: ResolvedDb) -> None:
    if resolved.temp_dir is not None:
        shutil.rmtree(resolved.temp_dir, ignore_errors=True)
    elif resolved.discard_after:
        delete_snapshot(resolved.db_path)


def snapshot_copy(src_db: Path, dest_db: Path, *, source: Path | None = None) -> SnapshotResult:
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    if dest_db.exists():
        dest_db.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(dest_db) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    t0 = time.perf_counter()
    copied: list[tuple[Path, int]] = []
    for src in (src_db, Path(str(src_db) + "-wal"), Path(str(src_db) + "-shm")):
        if not src.exists():
            continue
        dst = dest_db if src == src_db else Path(str(dest_db) + src.name[len(src_db.name) :])
        shutil.copy2(src, dst)
        copied.append((dst, dst.stat().st_size))

    result = SnapshotResult(path=dest_db, elapsed_sec=time.perf_counter() - t0, files=copied)
    if source is not None:
        write_snapshot_manifest(dest_db, source=source, fingerprint=source_fingerprint(source))
    return result


@dataclass
class PageStats:
    page_size: int
    page_count: int
    freelist_count: int
    file_bytes: int
    reclaimable_bytes: int
    live_bytes: int
    reclaimable_ratio: float


def page_stats(conn: sqlite3.Connection) -> PageStats:
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    file_bytes = page_count * page_size
    reclaimable = freelist * page_size
    return PageStats(
        page_size=page_size,
        page_count=page_count,
        freelist_count=freelist,
        file_bytes=file_bytes,
        reclaimable_bytes=reclaimable,
        live_bytes=file_bytes - reclaimable,
        reclaimable_ratio=round(reclaimable / file_bytes, 4) if file_bytes else 0.0,
    )


def quick_check(conn: sqlite3.Connection) -> str:
    try:
        return str(conn.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error as exc:
        return f"error: {exc}"
