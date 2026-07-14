from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .analyze import human_bytes

BLOB_KEY_RE_PREFIX = "agentKv:blob:"
CONTENT_KEY_PREFIX = "composer.content."


@dataclass
class FormatBucket:
    format: str
    count: int
    bytes: int
    detail: str = ""


@dataclass
class BlobProbeReport:
    prefix: str
    sampled: int
    total_keys: int
    total_bytes: int
    hash_matches_key: int
    hash_mismatch: int
    formats: list[FormatBucket] = field(default_factory=list)
    inner_formats: list[FormatBucket] = field(default_factory=list)
    size_min: int = 0
    size_median: int = 0
    size_max: int = 0
    notes: list[str] = field(default_factory=list)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_mostly_printable_utf8(data: bytes, *, threshold: float = 0.95) -> bool:
    if not data:
        return True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(ch.isprintable() or ch in "\n\r\t" for ch in text)
    return printable / len(text) >= threshold


def _classify_raw(data: bytes) -> tuple[str, str, bytes | None]:
    """Classify payload bytes. Returns (format, detail, decompressed_inner)."""
    if not data:
        return "empty", "", None

    if data[:1] in (b"{", b"["):
        try:
            json.loads(data.decode("utf-8"))
            return "json_utf8", "object" if data[:1] == b"{" else "array", None
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    if data[:2] == b"\x1f\x8b":
        try:
            inner = gzip.decompress(data)
            inner_fmt, inner_detail, _ = _classify_raw(inner)
            return "gzip", inner_fmt, inner
        except OSError:
            return "gzip", "decompress_failed", None

    if data[:2] in (b"\x78\x9c", b"\x78\x01", b"\x78\xda", b"\x78\x5e"):
        try:
            inner = zlib.decompress(data)
            inner_fmt, inner_detail, _ = _classify_raw(inner)
            return "zlib", inner_fmt, inner
        except zlib.error:
            return "zlib", "decompress_failed", None

    if data[:3] == b"BZh":
        return "bzip2", "", None

    if data[:4] == b"\x04\x22\x4d\x18":
        return "lz4_frame", "", None

    if data[:4] == b"RIFF":
        return "riff", data[8:12].decode("ascii", "ignore") or "unknown", None

    if data[:3] == b"\x89PN":
        return "png", "", None

    if data[:2] == b"\xff\xd8":
        return "jpeg", "", None

    if data[:4] == b"%PDF":
        return "pdf", "", None

    if _is_mostly_printable_utf8(data):
        return "utf8_text", "", None

    if data[0] & 0x80 and len(data) >= 2:
        return "msgpack_like", f"lead=0x{data[0]:02x}", None

    return "opaque_binary", f"lead={data[:8].hex()}", None


def _median(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _iter_blob_rows(
    conn: sqlite3.Connection,
    *,
    key_like: str,
    sample: int | None,
) -> Iterable[tuple[str, int, bytes]]:
    """Yield (key, full_size, header_bytes) without loading huge payloads."""
    sql = (
        "SELECT key, length(value), substr(value, 1, 512) "
        "FROM cursorDiskKV WHERE key LIKE ?"
    )
    params: list[object] = [key_like]
    if sample is not None:
        sql += " AND (rowid % 97) = 0 LIMIT ?"
        params.append(sample)
    for key, size, header in conn.execute(sql, params):
        if not isinstance(key, str) or header is None:
            continue
        full_size = int(size or 0)
        if isinstance(header, bytes):
            head = header
        elif isinstance(header, str):
            head = header.encode("utf-8", "surrogateescape")
        else:
            head = bytes(header)
        yield key, full_size, head


def _hash_matches_key(conn: sqlite3.Connection, key: str, *, max_bytes: int = 500_000) -> bool | None:
    """Return whether SHA-256(value) matches key hash; None if blob too large to check."""
    digest = None
    if key.startswith(BLOB_KEY_RE_PREFIX):
        digest = key.split(":", 2)[2]
    elif key.startswith(CONTENT_KEY_PREFIX):
        digest = key[len(CONTENT_KEY_PREFIX) :]
    if not digest:
        return None

    size = int(
        conn.execute(
            "SELECT length(value) FROM cursorDiskKV WHERE key = ?",
            (key,),
        ).fetchone()[0]
    )
    if size > max_bytes:
        return None

    row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    value = row[0]
    if isinstance(value, str):
        raw = value.encode("utf-8", "surrogateescape")
    elif isinstance(value, bytes):
        raw = value
    else:
        raw = bytes(value)
    return _sha256_hex(raw) == digest


def probe_blob_store(
    conn: sqlite3.Connection,
    *,
    prefix: str,
    key_like: str,
    sample: int | None = 500,
    extract_hash: Callable[[str], str | None] | None = None,
) -> BlobProbeReport:
    """Fingerprint blob formats for a key family without logging payloads."""
    extract_hash = extract_hash or (lambda _key: None)

    total_keys = int(
        conn.execute(
            "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE ?",
            (key_like,),
        ).fetchone()[0]
    )
    total_bytes = int(
        conn.execute(
            "SELECT COALESCE(SUM(length(value)), 0) FROM cursorDiskKV WHERE key LIKE ?",
            (key_like,),
        ).fetchone()[0]
    )

    format_counts: Counter[str] = Counter()
    format_bytes: Counter[str] = Counter()
    format_detail: dict[str, str] = {}
    inner_counts: Counter[str] = Counter()
    inner_bytes: Counter[str] = Counter()
    sizes: list[int] = []
    hash_matches = 0
    hash_mismatch = 0
    hash_skipped = 0
    hash_checks = 0
    max_hash_checks = min(40, sample or 40)
    sampled = 0

    for key, size, header in _iter_blob_rows(conn, key_like=key_like, sample=sample):
        sampled += 1
        sizes.append(size)

        if extract_hash(key) and hash_checks < max_hash_checks:
            hash_checks += 1
            match = _hash_matches_key(conn, key)
            if match is True:
                hash_matches += 1
            elif match is False:
                hash_mismatch += 1
            else:
                hash_skipped += 1

        probe_bytes = header
        fmt, detail, inner = _classify_raw(probe_bytes)
        format_counts[fmt] += 1
        format_bytes[fmt] += size
        if detail and fmt not in format_detail:
            format_detail[fmt] = detail

        if inner is not None:
            inner_fmt, inner_detail, _ = _classify_raw(inner)
            label = f"{inner_fmt}" if not inner_detail else f"{inner_fmt}:{inner_detail}"
            inner_counts[label] += 1
            inner_bytes[label] += len(inner)

    formats = [
        FormatBucket(
            format=fmt,
            count=format_counts[fmt],
            bytes=format_bytes[fmt],
            detail=format_detail.get(fmt, ""),
        )
        for fmt in format_counts
    ]
    formats.sort(key=lambda row: -row.bytes)

    inner_formats = [
        FormatBucket(format=fmt, count=inner_counts[fmt], bytes=inner_bytes[fmt])
        for fmt in inner_counts
    ]
    inner_formats.sort(key=lambda row: -row.bytes)

    notes: list[str] = []
    if sampled < total_keys:
        notes.append(
            f"Sampled {sampled:,} of {total_keys:,} keys (periodic rowid sample; first 512 bytes only)."
        )
    if hash_matches:
        notes.append(
            f"{hash_matches:,} sampled blob(s) have SHA-256(value) matching the key hash "
            "(content-addressed storage confirmed)."
        )
    if hash_mismatch:
        notes.append(
            f"{hash_mismatch:,} sampled blob(s) do not match key hash "
            "(key may use a different digest scheme or embed metadata)."
        )

    if hash_skipped:
        notes.append(
            f"{hash_skipped:,} sampled blob(s) skipped for hash verification (too large; limit 500 KB)."
        )

    return BlobProbeReport(
        prefix=prefix,
        sampled=sampled,
        total_keys=total_keys,
        total_bytes=total_bytes,
        hash_matches_key=hash_matches,
        hash_mismatch=hash_mismatch,
        formats=formats,
        inner_formats=inner_formats,
        size_min=min(sizes) if sizes else 0,
        size_median=_median(sizes),
        size_max=max(sizes) if sizes else 0,
        notes=notes,
    )


def probe_large_blob_headers(
    conn: sqlite3.Connection,
    *,
    key_like: str,
    prefix: str,
    top_n: int = 15,
) -> list[FormatBucket]:
    rows: list[FormatBucket] = []
    for key, size, header in conn.execute(
        """
        SELECT key, length(value), substr(value, 1, 512)
        FROM cursorDiskKV
        WHERE key LIKE ?
        ORDER BY length(value) DESC
        LIMIT ?
        """,
        (key_like, top_n),
    ):
        if not isinstance(key, str) or header is None:
            continue
        full_size = int(size or 0)
        head = header if isinstance(header, bytes) else str(header).encode("utf-8", "surrogateescape")
        fmt, detail, _ = _classify_raw(head)
        label = f"{fmt}" if not detail else f"{fmt} ({detail})"
        rows.append(FormatBucket(format=label, count=1, bytes=full_size, detail=key[:48] + "..."))
    return rows


def probe_agentkv_blobs(conn: sqlite3.Connection, *, sample: int | None = 500) -> BlobProbeReport:
    def extract_hash(key: str) -> str | None:
        if key.startswith(BLOB_KEY_RE_PREFIX):
            return key.split(":", 2)[2]
        return None

    return probe_blob_store(
        conn,
        prefix="agentKv:blob",
        key_like="agentKv:blob:%",
        sample=sample,
        extract_hash=extract_hash,
    )


def probe_composer_content_blobs(
    conn: sqlite3.Connection,
    *,
    sample: int | None = 200,
) -> BlobProbeReport:
    def extract_hash(key: str) -> str | None:
        if key.startswith(CONTENT_KEY_PREFIX):
            return key[len(CONTENT_KEY_PREFIX) :]
        return None

    return probe_blob_store(
        conn,
        prefix="composer.content",
        key_like="composer.content.%",
        sample=sample,
        extract_hash=extract_hash,
    )


def compare_content_overlap(
    conn: sqlite3.Connection,
    *,
    sample: int = 40,
) -> dict[str, int | float | str]:
    """Check whether overlapping hashes store identical bytes in both stores."""
    keys = [
        row[0]
        for row in conn.execute(
            """
            SELECT key FROM cursorDiskKV
            WHERE key LIKE 'agentKv:blob:%' AND (rowid % 97) = 0
            LIMIT ?
            """,
            (sample,),
        )
    ]
    checked = 0
    identical = 0
    for key in keys:
        if not isinstance(key, str):
            continue
        digest = key.split(":", 2)[2]
        content_key = f"{CONTENT_KEY_PREFIX}{digest}"
        row = conn.execute(
            """
            SELECT a.value = c.value
            FROM cursorDiskKV a, cursorDiskKV c
            WHERE a.key = ? AND c.key = ?
            """,
            (key, content_key),
        ).fetchone()
        if not row:
            continue
        checked += 1
        identical += int(bool(row[0]))
    if not checked:
        return {"pairs_checked": 0, "identical_bytes": 0, "note": "no overlapping hashes in sample"}

    return {
        "pairs_checked": checked,
        "identical_bytes": identical,
        "identical_ratio": identical / checked,
    }


@dataclass
class BlobProbeBundle:
    agentkv: BlobProbeReport
    composer_content: BlobProbeReport
    overlap: dict[str, int | float | str]
    agentkv_largest: list[FormatBucket] = field(default_factory=list)


def build_blob_probe_bundle(
    conn: sqlite3.Connection,
    *,
    agentkv_sample: int | None = 500,
    content_sample: int | None = 200,
    overlap_sample: int = 200,
) -> BlobProbeBundle:
    return BlobProbeBundle(
        agentkv=probe_agentkv_blobs(conn, sample=agentkv_sample),
        composer_content=probe_composer_content_blobs(conn, sample=content_sample),
        overlap=compare_content_overlap(conn, sample=overlap_sample),
        agentkv_largest=probe_large_blob_headers(
            conn,
            key_like="agentKv:blob:%",
            prefix="agentKv:blob",
        ),
    )


def blob_probe_to_dict(bundle: BlobProbeBundle) -> dict:
    from dataclasses import asdict

    return {
        "agentkv": asdict(bundle.agentkv),
        "composer_content": asdict(bundle.composer_content),
        "overlap": bundle.overlap,
        "agentkv_largest": [asdict(row) for row in bundle.agentkv_largest],
    }


def print_blob_probe_report(bundle: BlobProbeBundle) -> None:
    print("CursorScope blob format probe (payloads not shown)")
    for report in (bundle.agentkv, bundle.composer_content):
        print()
        print(f"=== {report.prefix} ===")
        print(f"keys: {report.total_keys:,}  bytes: {human_bytes(report.total_bytes)}")
        print(
            f"sampled: {report.sampled:,}  "
            f"sizes: min={human_bytes(report.size_min)} "
            f"median={human_bytes(report.size_median)} "
            f"max={human_bytes(report.size_max)}"
        )
        if report.hash_matches_key or report.hash_mismatch:
            print(
                f"hash check: {report.hash_matches_key:,} match  "
                f"{report.hash_mismatch:,} mismatch (SHA-256 vs key)"
            )
        print(f"{'Format':<22} {'Count':>8}  {'Bytes':>10}  Detail")
        print("-" * 64)
        for row in report.formats:
            detail = row.detail or ""
            print(
                f"{row.format:<22} {row.count:>8,}  "
                f"{human_bytes(row.bytes):>10}  {detail}"
            )
        if report.inner_formats:
            print("  after decompress:")
            for row in report.inner_formats:
                print(
                    f"    {row.format:<20} {row.count:>8,}  {human_bytes(row.bytes):>10}"
                )
        for note in report.notes:
            print(f"  Note: {note}")

    if bundle.agentkv_largest:
        print()
        print("=== agentKv largest blobs (header format) ===")
        print(f"{'Format':<28} {'Size':>10}  Key prefix")
        print("-" * 64)
        for row in bundle.agentkv_largest:
            print(f"{row.format:<28} {human_bytes(row.bytes):>10}  {row.detail}")

    print()
    print("=== agentKv vs composer.content overlap ===")
    for key, value in bundle.overlap.items():
        print(f"  {key}: {value}")
