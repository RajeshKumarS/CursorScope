from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


@dataclass
class SessionListRow:
    composer_id: str
    bytes: int
    name: str | None = None
    subtitle: str | None = None
    repo: str | None = None
    repo_confidence: str = "none"
    has_header: bool = False
    has_composer_data: bool = False
    breakdown: dict[str, int] = field(default_factory=dict)


@dataclass
class SessionsReport:
    source: str
    snapshot: str
    filter_no_header: bool
    total_matching: int
    sessions: list[SessionListRow]
    notes: list[str] = field(default_factory=list)



def build_sessions_report(
    db_path: Path,
    *,
    source: Path,
    top_n: int | None = 500,
    no_header: bool = False,
    repo_filter: str | None = None,
    resolver: WorkspaceResolver | None = None,
) -> SessionsReport:
    resolver = resolver or WorkspaceResolver()
    conn = open_ro(db_path)
    try:
        headers = load_headers(conn)
        header_ids = set(headers)
        storage = get_storage_scan(conn, db_path, progress=False, use_cache=True)
        by_composer = storage.by_composer
        composer_breakdown = storage.composer_breakdown

        rows: list[SessionListRow] = []
        for composer_id, total in by_composer.most_common():
            has_header = composer_id in header_ids
            if no_header and has_header:
                continue

            header = headers.get(composer_id)
            has_composer_data = (
                conn.execute(
                    "SELECT 1 FROM cursorDiskKV WHERE key = ?",
                    (f"composerData:{composer_id}",),
                ).fetchone()
                is not None
            )
            meta = header or (load_composer_metadata(conn, composer_id) if has_composer_data else {})

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

            rows.append(
                SessionListRow(
                    composer_id=composer_id,
                    bytes=total,
                    name=(header or meta or {}).get("name"),
                    subtitle=(header or meta or {}).get("subtitle"),
                    repo=repo,
                    repo_confidence=confidence,
                    has_header=has_header,
                    has_composer_data=has_composer_data,
                    breakdown=dict(composer_breakdown.get(composer_id, {})),
                )
            )

        notes: list[str] = []
        if no_header:
            notes.append(
                "Sessions without headers are usually historical; composer.composerHeaders tracks recent composers only."
            )
            notes.append(
                "Repo labels fall back to composerData when available; confidence may be lower without headers."
            )

        return SessionsReport(
            source=str(source),
            snapshot=str(db_path),
            filter_no_header=no_header,
            total_matching=len(rows),
            sessions=rows if top_n is None else rows[:top_n],
            notes=notes,
        )
    finally:
        conn.close()


def print_sessions_report(report: SessionsReport) -> None:
    title = "Sessions without headers" if report.filter_no_header else "Composer sessions"
    print(f"CursorScope {title.lower()}")
    print(f"source:   {report.source}")
    print(f"snapshot: {report.snapshot}")
    print(f"matching: {report.total_matching:,} session(s)")
    print()

    print(f"{'Size':>10}  {'Hdr':<3}  {'Conf':<6}  {'Name / subtitle / id'}")
    print("-" * 90)
    for row in report.sessions:
        label = row.name or row.subtitle or row.composer_id
        if row.name and row.subtitle:
            label = f"{row.name} - {row.subtitle}"
        if row.repo:
            label = f"{label} [{row.repo}]"
        hdr = "yes" if row.has_header else "no"
        print(f"{human_bytes(row.bytes):>10}  {hdr:<3}  {row.repo_confidence:<6}  {label}")
        if row.breakdown:
            parts = ", ".join(
                f"{k}={human_bytes(v)}"
                for k, v in sorted(row.breakdown.items(), key=lambda item: -item[1])
            )
            print(f"{'':>10}           {parts}")
            print(f"{'':>10}           drill-down: cursorscope session {row.composer_id}")

    if report.total_matching > len(report.sessions):
        print()
        print(f"Showing top {len(report.sessions)} of {report.total_matching:,}.")

    if report.notes:
        print()
        for note in report.notes:
            print(f"Note: {note}")


def sessions_report_to_dict(report: SessionsReport) -> dict:
    return asdict(report)
