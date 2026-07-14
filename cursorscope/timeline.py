from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .analyze import human_bytes, load_headers
from .db import open_ro
from .scan import get_storage_scan
from .workspace import (
    WorkspaceResolver,
    load_composer_metadata,
    resolve_repo,
    resolve_repo_from_fallback,
)


def normalize_timestamp(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    if ts < 0:
        return None
    return ts


def format_timestamp(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def month_key(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


@dataclass
class TimelineSession:
    composer_id: str
    bytes: int
    repo: str | None = None
    repo_confidence: str = "none"
    name: str | None = None
    subtitle: str | None = None
    created_at: int | None = None
    last_updated_at: int | None = None
    created_date: str | None = None
    last_updated_date: str | None = None
    has_header: bool = False
    is_archived: bool | None = None


@dataclass
class MonthBucket:
    month: str
    session_count: int
    bytes: int
    header_linked_bytes: int
    orphan_bytes: int
    unknown_date_bytes: int


@dataclass
class RepoActivity:
    repo: str
    session_count: int
    bytes: int
    first_seen: str | None = None
    last_seen: str | None = None
    confidence: str = "medium"


@dataclass
class TimelineReport:
    source: str
    snapshot: str
    generated_at: str
    sessions_total: int
    sessions_with_timestamps: int
    sessions_with_headers: int
    orphan_sessions: int
    orphan_bytes: int
    unknown_date_sessions: int
    unknown_date_bytes: int
    by_month: list[MonthBucket] = field(default_factory=list)
    repos: list[RepoActivity] = field(default_factory=list)
    sessions: list[TimelineSession] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_timeline_report(
    db_path: Path,
    *,
    source: Path,
    resolver: WorkspaceResolver | None = None,
    repo_filter: str | None = None,
    limit: int | None = 500,
    use_cache: bool = True,
) -> TimelineReport:
    resolver = resolver or WorkspaceResolver()
    conn = open_ro(db_path)
    try:
        headers = load_headers(conn)
        header_ids = set(headers)
        storage = get_storage_scan(conn, db_path, progress=False, use_cache=use_cache)
        by_composer = storage.by_composer

        sessions: list[TimelineSession] = []
        month_sessions: dict[str, list[TimelineSession]] = {}
        repo_map: dict[str, list[TimelineSession]] = {}

        for composer_id, total in by_composer.items():
            has_header = composer_id in header_ids
            header = headers.get(composer_id)
            meta = header
            if not meta and composer_id in storage.composer_data_ids:
                meta = load_composer_metadata(conn, composer_id)

            if header:
                repo, confidence = resolve_repo(header, resolver)
            elif meta:
                repo, confidence = resolve_repo_from_fallback(meta, resolver)
            else:
                repo, confidence = None, "low"

            if repo_filter is not None:
                row_repo = repo or "(unknown)"
                if row_repo != repo_filter:
                    continue

            created_at = normalize_timestamp(
                (header or meta or {}).get("createdAt") if (header or meta) else None
            )
            last_updated_at = normalize_timestamp(
                (header or meta or {}).get("lastUpdatedAt") if (header or meta) else None
            )
            event_ts = created_at or last_updated_at

            row = TimelineSession(
                composer_id=composer_id,
                bytes=total,
                repo=repo,
                repo_confidence=confidence,
                name=(header or meta or {}).get("name"),
                subtitle=(header or meta or {}).get("subtitle"),
                created_at=created_at,
                last_updated_at=last_updated_at,
                created_date=format_timestamp(created_at),
                last_updated_date=format_timestamp(last_updated_at),
                has_header=has_header,
                is_archived=(header or {}).get("isArchived") if header else None,
            )
            sessions.append(row)

            bucket = month_key(event_ts) or "(unknown)"
            month_sessions.setdefault(bucket, []).append(row)
            repo_label = repo or "(unknown)"
            repo_map.setdefault(repo_label, []).append(row)

        sessions.sort(
            key=lambda row: (row.created_at or row.last_updated_at or 0, row.bytes),
            reverse=True,
        )

        orphan_sessions = sum(1 for row in sessions if not row.has_header)
        orphan_bytes = sum(row.bytes for row in sessions if not row.has_header)
        with_ts = sum(1 for row in sessions if row.created_at or row.last_updated_at)
        unknown_date = sum(1 for row in sessions if not row.created_at and not row.last_updated_at)
        unknown_date_bytes = sum(
            row.bytes for row in sessions if not row.created_at and not row.last_updated_at
        )
        with_headers = sum(1 for row in sessions if row.has_header)

        listed = sessions if limit is None else sessions[:limit]

        by_month: list[MonthBucket] = []
        for month in sorted(month_sessions, key=lambda m: (m == "(unknown)", m)):
            rows = month_sessions[month]
            header_bytes = sum(r.bytes for r in rows if r.has_header)
            month_orphan_bytes = sum(r.bytes for r in rows if not r.has_header)
            unknown_bytes = sum(r.bytes for r in rows if month == "(unknown)")
            by_month.append(
                MonthBucket(
                    month=month,
                    session_count=len(rows),
                    bytes=sum(r.bytes for r in rows),
                    header_linked_bytes=header_bytes,
                    orphan_bytes=month_orphan_bytes,
                    unknown_date_bytes=unknown_bytes if month == "(unknown)" else 0,
                )
            )

        repos: list[RepoActivity] = []
        for repo, rows in repo_map.items():
            dated = [r for r in rows if r.created_at or r.last_updated_at]
            first_ts = min(
                (r.created_at or r.last_updated_at or 0 for r in dated),
                default=0,
            )
            last_ts = max(
                (r.last_updated_at or r.created_at or 0 for r in dated),
                default=0,
            )
            confidences = [r.repo_confidence for r in rows if r.repo_confidence != "none"]
            confidence = confidences[0] if confidences else "low"
            if any(c == "high" for c in confidences):
                confidence = "high"
            elif confidence != "high" and any(c == "medium" for c in confidences):
                confidence = "medium"
            repos.append(
                RepoActivity(
                    repo=repo,
                    session_count=len(rows),
                    bytes=sum(r.bytes for r in rows),
                    first_seen=format_timestamp(first_ts) if first_ts else None,
                    last_seen=format_timestamp(last_ts) if last_ts else None,
                    confidence=confidence,
                )
            )
        repos.sort(key=lambda row: -row.bytes)

        notes: list[str] = []
        if orphan_sessions:
            notes.append(
                f"{orphan_sessions:,} sessions lack composer.composerHeaders entries; "
                "timestamps may come from composerData fallback or be missing."
            )
        if unknown_date:
            notes.append(
                f"{unknown_date:,} sessions have storage but no createdAt/lastUpdatedAt; "
                f"they appear under month (unknown) ({human_bytes(unknown_date_bytes)})."
            )
        notes.append(
            "Timeline uses a single snapshot; byte sizes are current totals, not historical write times."
        )

        return TimelineReport(
            source=str(source),
            snapshot=str(db_path),
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            sessions_total=len(by_composer),
            sessions_with_timestamps=with_ts,
            sessions_with_headers=with_headers,
            orphan_sessions=orphan_sessions,
            orphan_bytes=orphan_bytes,
            unknown_date_sessions=unknown_date,
            unknown_date_bytes=unknown_date_bytes,
            by_month=by_month,
            repos=repos,
            sessions=listed,
            notes=notes,
        )
    finally:
        conn.close()


def timeline_report_to_dict(report: TimelineReport) -> dict:
    from dataclasses import asdict

    return asdict(report)


def print_timeline_report(report: TimelineReport) -> None:
    print("CursorScope timeline (Phase A - in-database)")
    print(f"source:   {report.source}")
    print(f"snapshot: {report.snapshot}")
    print(f"sessions: {report.sessions_total:,}  with timestamps: {report.sessions_with_timestamps:,}")
    print(
        f"headers:  {report.sessions_with_headers:,}  "
        f"orphans: {report.orphan_sessions:,} ({human_bytes(report.orphan_bytes)})"
    )
    print()
    print("Storage by month")
    print(f"{'Month':<12} {'Sessions':>9}  {'Header':>10}  {'Orphan':>10}  {'Total':>10}")
    print("-" * 58)
    for row in report.by_month:
        print(
            f"{row.month:<12} {row.session_count:>9,}  "
            f"{human_bytes(row.header_linked_bytes):>10}  "
            f"{human_bytes(row.orphan_bytes):>10}  "
            f"{human_bytes(row.bytes):>10}"
        )
    print()
    print("Top repositories by activity span")
    print(f"{'Repo':<40} {'Sessions':>8}  {'First':>12}  {'Last':>12}  {'Size':>10}")
    print("-" * 90)
    for row in report.repos[:15]:
        repo = row.repo if len(row.repo) <= 40 else "…" + row.repo[-37:]
        print(
            f"{repo:<40} {row.session_count:>8,}  "
            f"{(row.first_seen or '-'):>12}  {(row.last_seen or '-'):>12}  "
            f"{human_bytes(row.bytes):>10}"
        )
    print()
    print("Recent / largest sessions")
    print(f"{'Date':<12} {'Size':>10}  {'Hdr':>3}  Name / id")
    print("-" * 80)
    for row in report.sessions[:20]:
        date = row.created_date or row.last_updated_date or "(unknown)"
        label = row.name or row.subtitle or row.composer_id[:12]
        hdr = "yes" if row.has_header else "no"
        repo_suffix = f" [{row.repo}]" if row.repo else ""
        print(f"{date:<12} {human_bytes(row.bytes):>10}  {hdr:>3}  {label}{repo_suffix}")
    print()
    for note in report.notes:
        print(f"Note: {note}")
