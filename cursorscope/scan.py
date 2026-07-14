from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ProgressReporter = Callable[..., None]
COMPOSER_PREFIXES = (
    "bubbleId:",
    "checkpointId:",
    "composerData:",
    "messageRequestContext:",
    "codeBlockDiff:",
    "codeBlockPartialInlineDiffFates:",
)

ORPHAN_OBJECT_KINDS = (
    "bubbleId",
    "checkpointId",
    "messageRequestContext",
    "codeBlockDiff",
    "codeBlockPartialInlineDiffFates",
)


def classify_key(key: str) -> str:
    if not isinstance(key, str):
        return "(invalid key)"
    for prefix in COMPOSER_PREFIXES:
        if key.startswith(prefix):
            return prefix.rstrip(":")
    if key.startswith("agentKv:"):
        return "agentKv"
    if key.startswith("composer."):
        return "composer.*"
    if key.startswith("ofsContent:"):
        return "ofsContent"
    if key.startswith("inlineDiff"):
        return "inlineDiff*"
    if ":" in key:
        return key.split(":", 1)[0] + ":"
    return "(other)"


class ScanProgress:
    """Progress reporting for long cursorDiskKV scans (stderr and/or callback)."""

    def __init__(
        self,
        label: str,
        total: int,
        *,
        reporter: ProgressReporter | None = None,
        stderr: bool = False,
    ) -> None:
        self.label = label
        self.total = max(total, 1)
        self.reporter = reporter
        self.stderr = stderr
        self.start = time.perf_counter()
        self._last_emit = 0.0

    def _emit(self, current: int, *, cached: bool = False) -> None:
        elapsed = time.perf_counter() - self.start
        rate = current / elapsed if elapsed > 0 else 0.0
        eta = (self.total - current) / rate if rate > 0 and current < self.total else 0.0
        if self.reporter:
            self.reporter(
                current,
                self.total,
                self.label,
                cached=cached,
                eta_sec=None if cached else (0.0 if current >= self.total else eta),
            )
        if not self.stderr:
            return
        if cached:
            print(
                f"\r{self.label}: {self.total:,} rows in {elapsed:.1f}s (cached)   ",
                file=sys.stderr,
                flush=True,
            )
            return
        pct = 100.0 * current / self.total
        print(
            f"\r{self.label}: {current:,}/{self.total:,} ({pct:.0f}%) "
            f"elapsed {elapsed:.0f}s ETA {eta:.0f}s",
            end="",
            file=sys.stderr,
            flush=True,
        )

    def update(self, current: int) -> None:
        now = time.perf_counter()
        if current < self.total and now - self._last_emit < 0.25:
            return
        self._emit(current)
        self._last_emit = now

    def done(self, *, cached: bool = False) -> None:
        self._emit(self.total, cached=cached)


@dataclass
class StorageScan:
    by_type: Counter[str] = field(default_factory=Counter)
    by_type_count: Counter[str] = field(default_factory=Counter)
    by_composer: Counter[str] = field(default_factory=Counter)
    composer_breakdown: dict[str, Counter[str]] = field(default_factory=dict)
    composer_data_ids: set[str] = field(default_factory=set)
    orphaned_object_keys: Counter[str] = field(default_factory=Counter)
    kv_row_count: int = 0
    db_fingerprint: tuple[int, int] = (0, 0)

    def prefix_rows(self, known_prefixes: set[str]) -> list[tuple[str, int, int, bool]]:
        rows = [
            (
                label,
                self.by_type_count[label],
                self.by_type[label],
                label in known_prefixes,
            )
            for label in self.by_type
        ]
        rows.sort(key=lambda item: (-item[2], item[0]))
        return rows


_MEMORY_CACHE: dict[str, StorageScan] = {}


def db_fingerprint(db_path: Path) -> tuple[int, int]:
    stat = db_path.stat()
    return stat.st_mtime_ns, stat.st_size


def _cache_key(db_path: Path) -> str:
    mtime_ns, size = db_fingerprint(db_path)
    digest = hashlib.sha256(f"{db_path.resolve()}:{mtime_ns}:{size}".encode()).hexdigest()[:16]
    return digest


def default_scan_cache_dir() -> Path:
    return Path.home() / ".cursorscope" / "cache"


def _scan_cache_path(db_path: Path) -> Path:
    return default_scan_cache_dir() / f"scan-{_cache_key(db_path)}.json"


def _serialize_scan(scan: StorageScan) -> dict:
    return {
        "db_fingerprint": list(scan.db_fingerprint),
        "kv_row_count": scan.kv_row_count,
        "by_type": dict(scan.by_type),
        "by_type_count": dict(scan.by_type_count),
        "by_composer": dict(scan.by_composer),
        "composer_breakdown": {
            composer_id: dict(counter)
            for composer_id, counter in scan.composer_breakdown.items()
        },
        "composer_data_ids": sorted(scan.composer_data_ids),
        "orphaned_object_keys": dict(scan.orphaned_object_keys),
    }


def _deserialize_scan(data: dict) -> StorageScan:
    return StorageScan(
        by_type=Counter(data.get("by_type", {})),
        by_type_count=Counter(data.get("by_type_count", {})),
        by_composer=Counter(data.get("by_composer", {})),
        composer_breakdown={
            composer_id: Counter(counter)
            for composer_id, counter in data.get("composer_breakdown", {}).items()
        },
        composer_data_ids=set(data.get("composer_data_ids", [])),
        orphaned_object_keys=Counter(data.get("orphaned_object_keys", {})),
        kv_row_count=int(data.get("kv_row_count", 0)),
        db_fingerprint=tuple(data.get("db_fingerprint", (0, 0))),
    )


def load_cached_scan(db_path: Path) -> StorageScan | None:
    memory_key = _cache_key(db_path)
    if memory_key in _MEMORY_CACHE:
        return _MEMORY_CACHE[memory_key]

    cache_file = _scan_cache_path(db_path)
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    fingerprint = db_fingerprint(db_path)
    if tuple(data.get("db_fingerprint", ())) != fingerprint:
        return None

    scan = _deserialize_scan(data)
    _MEMORY_CACHE[memory_key] = scan
    return scan


def save_cached_scan(db_path: Path, scan: StorageScan) -> None:
    memory_key = _cache_key(db_path)
    _MEMORY_CACHE[memory_key] = scan
    cache_file = _scan_cache_path(db_path)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(_serialize_scan(scan), indent=2), encoding="utf-8")


def scan_storage(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    composer_data_ids: set[str] | None = None,
    progress: bool = False,
    on_progress: ProgressReporter | None = None,
) -> StorageScan:
    composer_data_ids = set(composer_data_ids or ())
    scan = StorageScan(db_fingerprint=db_fingerprint(db_path))
    scan.composer_data_ids = set(composer_data_ids)

    kv_rows = int(conn.execute("SELECT COUNT(*) FROM cursorDiskKV").fetchone()[0])
    scan.kv_row_count = kv_rows
    tracker: ScanProgress | None = None
    if on_progress or progress:
        tracker = ScanProgress(
            "Scanning cursorDiskKV",
            kv_rows,
            reporter=on_progress,
            stderr=progress and on_progress is None,
        )

    processed = 0
    for key, vlen in conn.execute("SELECT key, length(value) FROM cursorDiskKV"):
        processed += 1
        if tracker and processed % 5000 == 0:
            tracker.update(processed)

        if not isinstance(key, str):
            scan.by_type["(invalid key)"] += int(vlen or 0)
            scan.by_type_count["(invalid key)"] += 1
            continue

        size = int(vlen or 0)
        label = classify_key(key)
        scan.by_type[label] += size
        scan.by_type_count[label] += 1

        for prefix in COMPOSER_PREFIXES:
            if not key.startswith(prefix):
                continue
            parts = key.split(":")
            if len(parts) < 2:
                break
            composer_id = parts[1]
            kind = prefix.rstrip(":")
            scan.by_composer[composer_id] += size
            scan.composer_breakdown.setdefault(composer_id, Counter())[kind] += size
            if kind == "composerData":
                scan.composer_data_ids.add(composer_id)
            elif kind in ORPHAN_OBJECT_KINDS and composer_id not in composer_data_ids:
                scan.orphaned_object_keys[kind] += 1
            break

    if tracker:
        tracker.update(kv_rows)
        tracker.done()

    return scan


def cleanup_scan_cache(cache_dir: Path | None = None) -> list[Path]:
    cache_dir = cache_dir or default_scan_cache_dir()
    removed: list[Path] = []
    if not cache_dir.is_dir():
        return removed
    for path in cache_dir.glob("scan-*.json"):
        path.unlink()
        removed.append(path)
    _MEMORY_CACHE.clear()
    return removed


def load_composer_data_ids(conn: sqlite3.Connection) -> set[str]:
    ids: set[str] = set()
    for (key,) in conn.execute(
        "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ):
        if isinstance(key, str) and key.startswith("composerData:"):
            ids.add(key.split(":", 1)[1])
    return ids


def get_storage_scan(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    progress: bool = False,
    use_cache: bool = True,
    on_progress: ProgressReporter | None = None,
) -> StorageScan:
    if use_cache:
        cached = load_cached_scan(db_path)
        if cached is not None:
            if on_progress:
                on_progress(
                    cached.kv_row_count,
                    cached.kv_row_count,
                    "Scanning cursorDiskKV",
                    cached=True,
                    eta_sec=0.0,
                )
            elif progress:
                tracker = ScanProgress("Scanning cursorDiskKV", cached.kv_row_count, stderr=True)
                tracker.done(cached=True)
            return cached

    composer_data_ids = load_composer_data_ids(conn)
    scan = scan_storage(
        conn,
        db_path,
        composer_data_ids=composer_data_ids,
        progress=progress,
        on_progress=on_progress,
    )
    scan.composer_data_ids = composer_data_ids
    if use_cache:
        save_cached_scan(db_path, scan)
    return scan
