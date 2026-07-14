from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field

BLOB_KEY_RE = re.compile(r"^agentKv:blob:([a-f0-9]{64})$")
CONTENT_KEY_RE = re.compile(r"^composer\.content\.([a-f0-9]{64})$")
STRUCTURAL_REF_RE = re.compile(rb"agentKv:blob:([a-f0-9]{64})")

REFERRER_PREFIXES = (
    "bubbleId:",
    "checkpointId:",
    "composerData:",
    "messageRequestContext:",
    "codeBlockDiff:",
    "codeBlockPartialInlineDiffFates:",
)


@dataclass
class AgentKvReport:
    total_blobs: int = 0
    total_bytes: int = 0
    referenced_blobs: int = 0
    referenced_bytes_unique: int = 0
    unreferenced_blobs: int = 0
    unreferenced_bytes: int = 0
    structural_reference_rows: int = 0
    composer_content_blobs: int = 0
    composer_content_bytes: int = 0
    overlap_blobs: int = 0
    overlap_bytes: int = 0
    attributed_by_composer: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def composer_id_from_key(key: str) -> str | None:
    if not isinstance(key, str):
        return None
    parts = key.split(":")
    if parts[0] == "composerData" and len(parts) >= 2:
        return parts[1]
    if parts[0] in {p.rstrip(":") for p in REFERRER_PREFIXES if p != "composerData:"} and len(parts) >= 2:
        return parts[1]
    return None


def load_blob_index(conn: sqlite3.Connection) -> dict[str, int]:
    blobs: dict[str, int] = {}
    for key, size in conn.execute(
        "SELECT key, length(value) FROM cursorDiskKV WHERE key LIKE 'agentKv:blob:%'"
    ):
        if not isinstance(key, str):
            continue
        match = BLOB_KEY_RE.match(key)
        if match:
            blobs[match.group(1)] = int(size or 0)
    return blobs


def load_composer_content_index(conn: sqlite3.Connection) -> dict[str, int]:
    content: dict[str, int] = {}
    for key, size in conn.execute(
        "SELECT key, length(value) FROM cursorDiskKV WHERE key LIKE 'composer.content.%'"
    ):
        if not isinstance(key, str):
            continue
        match = CONTENT_KEY_RE.match(key)
        if match:
            content[match.group(1)] = int(size or 0)
    return content


def scan_structural_references(
    conn: sqlite3.Connection,
    blob_hashes: set[str],
    *,
    progress: bool = False,
) -> tuple[dict[str, set[str]], int]:
    """Map composer_id -> blob hashes referenced via agentKv:blob:HASH in payloads."""
    composer_refs: dict[str, set[str]] = defaultdict(set)
    structural_rows = 0
    prefix_query = " OR ".join("key LIKE ?" for _ in REFERRER_PREFIXES)
    prefix_params = [f"{prefix}%" for prefix in REFERRER_PREFIXES]

    # Pre-filter in SQLite; almost all rows lack structural agentKv refs.
    rows = conn.execute(
        f"""
        SELECT key, value FROM cursorDiskKV
        WHERE ({prefix_query}) AND value LIKE '%agentKv:blob:%'
        """,
        prefix_params,
    )
    for key, value in rows:
        composer_id = composer_id_from_key(key)
        if not composer_id or value is None:
            continue

        raw = value if isinstance(value, bytes) else value.encode("utf-8", "surrogateescape")
        found = STRUCTURAL_REF_RE.findall(raw)
        if not found:
            continue

        structural_rows += 1
        for digest in found:
            hash_str = digest.decode("ascii")
            if hash_str in blob_hashes:
                composer_refs[composer_id].add(hash_str)

    if progress:
        print(
            f"  agentKv structural scan: {structural_rows} referrer row(s) with matches",
            file=sys.stderr,
        )

    return composer_refs, structural_rows


def build_agentkv_report(
    conn: sqlite3.Connection,
    *,
    progress: bool = False,
) -> AgentKvReport:
    blobs = load_blob_index(conn)
    content = load_composer_content_index(conn)
    blob_hashes = set(blobs)

    if progress:
        print("Scanning session payloads for agentKv:blob references ...", file=sys.stderr)

    composer_refs, structural_rows = scan_structural_references(
        conn, blob_hashes, progress=progress
    )

    referenced_hashes: set[str] = set()
    for hashes in composer_refs.values():
        referenced_hashes |= hashes

    attributed_by_composer: dict[str, int] = {}
    for composer_id, hashes in composer_refs.items():
        attributed_by_composer[composer_id] = sum(blobs[h] for h in hashes)

    overlap_hashes = set(blobs) & set(content)
    total_bytes = sum(blobs.values())
    referenced_bytes = sum(blobs[h] for h in referenced_hashes)
    unreferenced_bytes = total_bytes - referenced_bytes

    report = AgentKvReport(
        total_blobs=len(blobs),
        total_bytes=total_bytes,
        referenced_blobs=len(referenced_hashes),
        referenced_bytes_unique=referenced_bytes,
        unreferenced_blobs=len(blobs) - len(referenced_hashes),
        unreferenced_bytes=unreferenced_bytes,
        structural_reference_rows=structural_rows,
        composer_content_blobs=len(content),
        composer_content_bytes=sum(content.values()),
        overlap_blobs=len(overlap_hashes),
        overlap_bytes=sum(blobs[h] for h in overlap_hashes),
        attributed_by_composer=attributed_by_composer,
    )

    if report.referenced_blobs == 0:
        report.notes.append(
            "No structural agentKv:blob references found in session payloads on this database."
        )
    elif report.unreferenced_bytes > 0:
        report.notes.append(
            "Most agentKv blobs have no structural session reference in cursorDiskKV; "
            "unreferenced bytes may be shared, stale, or use an encoding not yet decoded."
        )

    if report.composer_content_bytes > 0:
        report.notes.append(
            "composer.content.<hash> is a related content-addressed store; "
            f"{report.overlap_blobs} hashes overlap agentKv blobs."
        )

    if sum(attributed_by_composer.values()) > referenced_bytes:
        report.notes.append(
            "Per-session agentKv totals sum blob bytes per reference; shared blobs may be counted in multiple sessions."
        )

    return report


def scan_session_blob_refs(
    conn: sqlite3.Connection,
    composer_id: str,
    blobs: dict[str, int] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Return (breakdown with agentKv bytes, list of referenced hashes) for one session."""
    if blobs is None:
        blobs = load_blob_index(conn)
    blob_hashes = set(blobs)

    patterns: list[str] = [f"composerData:{composer_id}"]
    patterns.extend(f"{prefix}{composer_id}:%" for prefix in REFERRER_PREFIXES if prefix != "composerData:")
    key_query = " OR ".join("key LIKE ?" if p.endswith("%") else "key = ?" for p in patterns)

    referenced: set[str] = set()
    for key, value in conn.execute(
        f"""
        SELECT key, value FROM cursorDiskKV
        WHERE ({key_query}) AND value LIKE '%agentKv:blob:%'
        """,
        patterns,
    ):
        if value is None:
            continue
        raw = value if isinstance(value, bytes) else value.encode("utf-8", "surrogateescape")
        for digest in STRUCTURAL_REF_RE.findall(raw):
            hash_str = digest.decode("ascii")
            if hash_str in blob_hashes:
                referenced.add(hash_str)

    bytes_total = sum(blobs[h] for h in referenced)
    breakdown = {"agentKv": bytes_total} if bytes_total else {}
    return breakdown, sorted(referenced)
