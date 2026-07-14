from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agentkv import load_blob_index, scan_session_blob_refs
from .analyze import human_bytes, load_headers
from .db import open_ro
from .workspace import WorkspaceResolver, resolve_repo, uri_to_path

SESSION_PREFIXES = (
    "bubbleId:",
    "checkpointId:",
    "composerData:",
    "messageRequestContext:",
    "codeBlockDiff:",
    "codeBlockPartialInlineDiffFates:",
)


@dataclass
class FileRef:
    path: str
    source: str


@dataclass
class BubbleRef:
    bubble_id: str
    bytes: int
    bubble_type: int | None = None


@dataclass
class CheckpointRef:
    checkpoint_id: str
    bytes: int


@dataclass
class SessionReport:
    composer_id: str
    found: bool
    source: str
    snapshot: str
    name: str | None = None
    subtitle: str | None = None
    repo: str | None = None
    repo_confidence: str = "none"
    created_at: int | None = None
    last_updated_at: int | None = None
    files_changed_count: int | None = None
    is_archived: bool | None = None
    storage_breakdown: dict[str, int] = field(default_factory=dict)
    total_bytes: int = 0
    files: list[FileRef] = field(default_factory=list)
    bubbles: list[BubbleRef] = field(default_factory=list)
    checkpoints: list[CheckpointRef] = field(default_factory=list)
    bubble_count: int = 0
    checkpoint_count: int = 0
    composer_data_bytes: int = 0
    agentkv_bytes: int = 0
    agentkv_hashes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def decode_value(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")



def add_file(files: list[FileRef], seen: set[str], path: str | None, source: str) -> None:
    if not path:
        return
    path = path.strip()
    if not path or path in seen:
        return
    seen.add(path)
    files.append(FileRef(path=path, source=source))


def extract_files_from_composer_data(obj: dict[str, Any]) -> list[FileRef]:
    files: list[FileRef] = []
    seen: set[str] = set()

    for source_key in ("newlyCreatedFiles", "addedFiles", "removedFiles"):
        for item in obj.get(source_key) or []:
            if isinstance(item, dict):
                add_file(files, seen, uri_to_path(item.get("uri")), source_key)
            elif isinstance(item, str):
                add_file(files, seen, uri_to_path(item), source_key)

    for uri in obj.get("allAttachedFileCodeChunksUris") or []:
        add_file(files, seen, uri_to_path(uri), "allAttachedFileCodeChunksUris")

    code_blocks = obj.get("codeBlockData")
    if isinstance(code_blocks, dict):
        for path in code_blocks:
            add_file(files, seen, str(path), "codeBlockData")

    context = obj.get("context")
    if isinstance(context, dict):
        for source_key in ("fileSelections", "selections", "folderSelections"):
            for item in context.get(source_key) or []:
                if isinstance(item, dict):
                    add_file(files, seen, uri_to_path(item.get("uri")), f"context.{source_key}")
                    add_file(files, seen, uri_to_path(item.get("path")), f"context.{source_key}")

    subtitle = obj.get("subtitle")
    if isinstance(subtitle, str) and subtitle.lower().startswith("edited "):
        for part in subtitle[7:].split(","):
            name = part.strip()
            if name:
                add_file(files, seen, name, "subtitle")

    return files


def resolve_composer_id(conn: sqlite3.Connection, token: str) -> str | None:
    token = token.strip()
    if not token:
        return None

    exact = conn.execute(
        "SELECT 1 FROM cursorDiskKV WHERE key = ?",
        (f"composerData:{token}",),
    ).fetchone()
    if exact:
        return token

    rows = conn.execute(
        """
        SELECT DISTINCT substr(key, 14) AS cid
        FROM cursorDiskKV
        WHERE key LIKE 'composerData:%' AND substr(key, 14) LIKE ?
        """,
        (f"{token}%",),
    ).fetchall()
    ids = [r[0] for r in rows if r[0]]
    if len(ids) == 1:
        return ids[0]
    if len(ids) > 1:
        raise ValueError(f"ambiguous composer id prefix {token!r}; matches {len(ids)} sessions")

    # Fall back to any keyed object for this composer
    rows = conn.execute(
        """
        SELECT DISTINCT substr(key, instr(key, ':') + 1, 36) AS cid
        FROM cursorDiskKV
        WHERE key LIKE ? AND substr(key, instr(key, ':') + 1, 36) LIKE ?
        LIMIT 5
        """,
        ("bubbleId:%", f"{token}%"),
    ).fetchall()
    ids = [r[0] for r in rows if r[0]]
    if len(ids) == 1:
        return ids[0]
    if len(ids) > 1:
        raise ValueError(f"ambiguous composer id prefix {token!r}; matches {len(ids)} sessions")
    return None


def load_composer_data(conn: sqlite3.Connection, composer_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ?",
        (f"composerData:{composer_id}",),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(decode_value(row[0]))
    except json.JSONDecodeError:
        return None


def bubble_headers_from_composer_data(obj: dict[str, Any] | None) -> dict[str, int]:
    if not obj:
        return {}
    headers = obj.get("fullConversationHeadersOnly") or []
    out: dict[str, int] = {}
    if isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict) and item.get("bubbleId") is not None:
                out[str(item["bubbleId"])] = item.get("type")
    return out


def scan_session_objects(
    conn: sqlite3.Connection,
    composer_id: str,
) -> tuple[dict[str, int], list[tuple[str, int]], list[tuple[str, int]]]:
    breakdown: dict[str, int] = {}
    bubbles: list[tuple[str, int]] = []
    checkpoints: list[tuple[str, int]] = []

    patterns: list[str] = [f"composerData:{composer_id}"]
    patterns.extend(f"{prefix}{composer_id}:%" for prefix in SESSION_PREFIXES if prefix != "composerData:")
    query = " OR ".join("key LIKE ?" if p.endswith("%") else "key = ?" for p in patterns)
    params: list[str] = []
    for pattern in patterns:
        params.append(pattern)

    for key, vlen in conn.execute(
        f"SELECT key, length(value) FROM cursorDiskKV WHERE {query}",
        params,
    ):
        if not isinstance(key, str):
            continue
        size = int(vlen or 0)
        parts = key.split(":")
        if len(parts) < 2:
            continue
        kind = parts[0]
        breakdown[kind] = breakdown.get(kind, 0) + size
        if kind == "bubbleId" and len(parts) >= 3:
            bubbles.append((parts[2], size))
        elif kind == "checkpointId" and len(parts) >= 3:
            checkpoints.append((parts[2], size))

    return breakdown, bubbles, checkpoints


def build_session_report(
    db_path: Path,
    composer_token: str,
    *,
    source: Path,
    top_n: int = 20,
    resolver: WorkspaceResolver | None = None,
    scan_agentkv: bool = True,
) -> SessionReport:
    resolver = resolver or WorkspaceResolver()
    conn = open_ro(db_path)
    try:
        try:
            composer_id = resolve_composer_id(conn, composer_token)
        except ValueError as exc:
            return SessionReport(
                composer_id=composer_token,
                found=False,
                source=str(source),
                snapshot=str(db_path),
                notes=[str(exc)],
            )

        if not composer_id:
            return SessionReport(
                composer_id=composer_token,
                found=False,
                source=str(source),
                snapshot=str(db_path),
                notes=[f"no session found for {composer_token!r}"],
            )

        headers = load_headers(conn)
        header = headers.get(composer_id)
        composer_data = load_composer_data(conn, composer_id)
        breakdown, bubble_rows, checkpoint_rows = scan_session_objects(conn, composer_id)

        agentkv_breakdown: dict[str, int] = {}
        agentkv_hashes: list[str] = []
        if scan_agentkv:
            blobs = load_blob_index(conn)
            agentkv_breakdown, agentkv_hashes = scan_session_blob_refs(
                conn, composer_id, blobs
            )
            for kind, size in agentkv_breakdown.items():
                breakdown[kind] = breakdown.get(kind, 0) + size

        report = SessionReport(
            composer_id=composer_id,
            found=True,
            source=str(source),
            snapshot=str(db_path),
            storage_breakdown=breakdown,
            total_bytes=sum(breakdown.values()),
            composer_data_bytes=breakdown.get("composerData", 0),
            agentkv_bytes=agentkv_breakdown.get("agentKv", 0),
            agentkv_hashes=agentkv_hashes,
            bubble_count=len(bubble_rows),
            checkpoint_count=len(checkpoint_rows),
        )

        if header:
            report.name = header.get("name")
            report.subtitle = header.get("subtitle")
            report.created_at = header.get("createdAt")
            report.last_updated_at = header.get("lastUpdatedAt")
            report.files_changed_count = header.get("filesChangedCount")
            report.is_archived = header.get("isArchived")
            repo, confidence = resolve_repo(header, resolver)
            report.repo = repo
            report.repo_confidence = confidence

        if composer_data:
            if not report.name:
                report.name = composer_data.get("name")
            if not report.subtitle:
                report.subtitle = composer_data.get("subtitle")
            if report.created_at is None:
                report.created_at = composer_data.get("createdAt")
            if report.last_updated_at is None:
                report.last_updated_at = composer_data.get("lastUpdatedAt")
            if report.files_changed_count is None:
                report.files_changed_count = composer_data.get("filesChangedCount")
            if not report.repo or report.repo == "(unknown)":
                repo, confidence = resolve_repo(composer_data, resolver)
                report.repo = repo
                report.repo_confidence = confidence
            report.files = extract_files_from_composer_data(composer_data)
        elif header and header.get("subtitle"):
            report.files = extract_files_from_composer_data({"subtitle": header.get("subtitle")})

        bubble_types = bubble_headers_from_composer_data(composer_data)
        bubble_rows.sort(key=lambda item: item[1], reverse=True)
        report.bubbles = [
            BubbleRef(
                bubble_id=bubble_id,
                bytes=size,
                bubble_type=bubble_types.get(bubble_id),
            )
            for bubble_id, size in bubble_rows[:top_n]
        ]

        checkpoint_rows.sort(key=lambda item: item[1], reverse=True)
        report.checkpoints = [
            CheckpointRef(checkpoint_id=checkpoint_id, bytes=size)
            for checkpoint_id, size in checkpoint_rows[:top_n]
        ]

        if report.bubble_count > len(report.bubbles):
            report.notes.append(
                f"showing top {len(report.bubbles)} of {report.bubble_count} bubbles by size"
            )
        if report.checkpoint_count > len(report.checkpoints):
            report.notes.append(
                f"showing top {len(report.checkpoints)} of {report.checkpoint_count} checkpoints by size"
            )
        if scan_agentkv and report.agentkv_bytes == 0:
            report.notes.append(
                "no structural agentKv:blob references found in this session's payloads"
            )
        elif scan_agentkv and report.agentkv_hashes:
            report.notes.append(
                f"agentKv attribution uses {len(report.agentkv_hashes)} structurally referenced blob(s)"
            )
        if not composer_data:
            report.notes.append("composerData row missing; file list may be incomplete")
        elif bubble_types:
            report.notes.append(
                f"bubble types available for {len(bubble_types)} entries via fullConversationHeadersOnly"
            )

        return report
    finally:
        conn.close()


def print_session_report(report: SessionReport) -> None:
    if not report.found:
        print(f"Session not found: {report.composer_id}")
        for note in report.notes:
            print(f"  {note}")
        return

    print("CursorScope session")
    print(f"composer: {report.composer_id}")
    if report.name:
        print(f"name:     {report.name}")
    if report.subtitle:
        print(f"subtitle: {report.subtitle}")
    if report.repo:
        print(f"repo:     {report.repo} ({report.repo_confidence} confidence)")
    if report.created_at is not None:
        print(f"created:  {report.created_at}")
    if report.last_updated_at is not None:
        print(f"updated:  {report.last_updated_at}")
    if report.files_changed_count is not None:
        print(f"files changed (metadata): {report.files_changed_count}")
    if report.is_archived is not None:
        print(f"archived: {report.is_archived}")
    print(f"total:    {human_bytes(report.total_bytes)}")
    print()

    print("Storage breakdown")
    for kind, size in sorted(report.storage_breakdown.items(), key=lambda item: -item[1]):
        print(f"  {kind:<36} {human_bytes(size):>10}")
    if report.agentkv_hashes:
        print(f"  {'agentKv refs':<36} {len(report.agentkv_hashes):>10} blobs")
    print()

    print(f"Referenced files ({len(report.files)})")
    if report.files:
        print(f"{'Source':<32}  Path")
        print("-" * 100)
        for file_ref in report.files[:50]:
            print(f"{file_ref.source:<32}  {file_ref.path}")
        if len(report.files) > 50:
            print(f"  ... and {len(report.files) - 50} more")
    else:
        print("  (none extracted)")
    print()

    print(f"Largest bubbles ({report.bubble_count} total)")
    if report.bubbles:
        print(f"{'Size':>10}  {'Type':>5}  Bubble ID")
        print("-" * 60)
        for bubble in report.bubbles:
            type_label = "" if bubble.bubble_type is None else str(bubble.bubble_type)
            print(f"{human_bytes(bubble.bytes):>10}  {type_label:>5}  {bubble.bubble_id}")
    else:
        print("  (none)")
    print()

    print(f"Largest checkpoints ({report.checkpoint_count} total)")
    if report.checkpoints:
        print(f"{'Size':>10}  Checkpoint ID")
        print("-" * 60)
        for checkpoint in report.checkpoints:
            print(f"{human_bytes(checkpoint.bytes):>10}  {checkpoint.checkpoint_id}")
    else:
        print("  (none)")

    if report.notes:
        print()
        for note in report.notes:
            print(f"Note: {note}")


def session_report_to_dict(report: SessionReport) -> dict[str, Any]:
    return asdict(report)
