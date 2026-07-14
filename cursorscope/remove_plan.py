from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .agentkv import load_blob_index, scan_structural_references
from .analyze import human_bytes, load_headers
from .db import open_ro
from .scan import get_storage_scan
from .session import SESSION_PREFIXES
from .timeline import format_timestamp, normalize_timestamp
from .workspace import (
    WorkspaceResolver,
    load_composer_metadata,
    resolve_repo,
    resolve_repo_from_fallback,
)


@dataclass
class PlannedSession:
    composer_id: str
    repo: str
    repo_confidence: str
    bytes: int
    has_header: bool
    has_composer_data: bool
    name: str | None = None
    subtitle: str | None = None
    created_date: str | None = None
    last_updated_date: str | None = None
    breakdown: dict[str, int] = field(default_factory=dict)
    agentkv_hashes: list[str] = field(default_factory=list)
    agentkv_bytes: int = 0


@dataclass
class RemovePlan:
    source: str
    database: str
    repos_requested: list[str]
    repos_matched: list[str]
    repos_unmatched: list[str]
    sessions: list[PlannedSession]
    session_count: int = 0
    attributed_bytes: int = 0
    kv_keys_estimate: int = 0
    agentkv_attributed_bytes: int = 0
    agentkv_exclusive_bytes: int = 0
    agentkv_shared_bytes: int = 0
    low_confidence_sessions: int = 0
    without_header_sessions: int = 0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _repo_matches(label: str, patterns: list[str], *, match_mode: str) -> bool:
    if match_mode == "suffix":
        normalized = label.replace("/", "\\").lower()
        return any(
            normalized.endswith(pattern.replace("/", "\\").lower())
            or pattern.replace("/", "\\").lower() in normalized
            for pattern in patterns
        )
    return label in patterns


def _count_session_keys(conn: sqlite3.Connection, composer_id: str) -> int:
    patterns: list[str] = [f"composerData:{composer_id}"]
    patterns.extend(
        f"{prefix}{composer_id}:%" for prefix in SESSION_PREFIXES if prefix != "composerData:"
    )
    query = " OR ".join("key LIKE ?" if pattern.endswith("%") else "key = ?" for pattern in patterns)
    row = conn.execute(
        f"SELECT COUNT(*) FROM cursorDiskKV WHERE {query}",
        patterns,
    ).fetchone()
    return int(row[0] if row else 0)


def build_remove_plan(
    db_path: Path,
    *,
    source: Path,
    repos: list[str],
    resolver: WorkspaceResolver | None = None,
    match_mode: str = "exact",
    use_cache: bool = True,
    scan_agentkv: bool = True,
) -> RemovePlan:
    if not repos:
        raise ValueError("at least one --repo is required")

    resolver = resolver or WorkspaceResolver()
    requested = list(dict.fromkeys(repos))
    conn = open_ro(db_path)
    try:
        headers = load_headers(conn)
        header_ids = set(headers)
        storage = get_storage_scan(conn, db_path, progress=False, use_cache=use_cache)
        by_composer = storage.by_composer
        composer_breakdown = storage.composer_breakdown

        sessions_by_repo: dict[str, list[PlannedSession]] = {}

        for composer_id, total in by_composer.items():
            header = headers.get(composer_id)
            has_composer_data = composer_id in storage.composer_data_ids
            meta = header or (
                load_composer_metadata(conn, composer_id) if has_composer_data else {}
            )

            if header:
                repo, confidence = resolve_repo(header, resolver)
            elif meta:
                repo, confidence = resolve_repo_from_fallback(meta, resolver)
            else:
                repo, confidence = "(unknown)", "low"

            if not _repo_matches(repo, requested, match_mode=match_mode):
                continue

            created_at = normalize_timestamp(
                (header or meta or {}).get("createdAt") if (header or meta) else None
            )
            last_updated_at = normalize_timestamp(
                (header or meta or {}).get("lastUpdatedAt") if (header or meta) else None
            )

            sessions_by_repo.setdefault(repo, []).append(
                PlannedSession(
                    composer_id=composer_id,
                    repo=repo,
                    repo_confidence=confidence,
                    bytes=total,
                    has_header=composer_id in header_ids,
                    has_composer_data=has_composer_data,
                    name=(header or meta or {}).get("name"),
                    subtitle=(header or meta or {}).get("subtitle"),
                    created_date=format_timestamp(created_at),
                    last_updated_date=format_timestamp(last_updated_at),
                    breakdown=dict(composer_breakdown.get(composer_id, {})),
                )
            )

        matched_repos = sorted(sessions_by_repo)
        if match_mode == "exact":
            unmatched = [repo for repo in requested if repo not in matched_repos]
        else:
            unmatched = [
                repo
                for repo in requested
                if not any(_repo_matches(matched, [repo], match_mode="suffix") for matched in matched_repos)
            ]

        sessions = [
            session
            for repo in matched_repos
            for session in sorted(sessions_by_repo[repo], key=lambda row: -row.bytes)
        ]

        blobs = load_blob_index(conn) if scan_agentkv else {}
        composer_refs: dict[str, set[str]] = {}
        if scan_agentkv and blobs:
            composer_refs, _ = scan_structural_references(conn, set(blobs))
            for session in sessions:
                hashes = sorted(composer_refs.get(session.composer_id, set()))
                session.agentkv_hashes = hashes
                session.agentkv_bytes = sum(blobs[h] for h in hashes)

        delete_ids = {session.composer_id for session in sessions}
        delete_hashes: set[str] = set()
        keep_hashes: set[str] = set()
        for composer_id, hashes in composer_refs.items():
            if composer_id in delete_ids:
                delete_hashes |= hashes
            else:
                keep_hashes |= hashes
        exclusive_hashes = delete_hashes - keep_hashes
        shared_hashes = delete_hashes & keep_hashes

        kv_keys = sum(_count_session_keys(conn, session.composer_id) for session in sessions)

        plan = RemovePlan(
            source=str(source.resolve()),
            database=str(db_path.resolve()),
            repos_requested=requested,
            repos_matched=matched_repos,
            repos_unmatched=unmatched,
            sessions=sessions,
            session_count=len(sessions),
            attributed_bytes=sum(session.bytes for session in sessions),
            kv_keys_estimate=kv_keys,
            agentkv_attributed_bytes=sum(blobs[h] for h in delete_hashes) if blobs else 0,
            agentkv_exclusive_bytes=sum(blobs[h] for h in exclusive_hashes) if blobs else 0,
            agentkv_shared_bytes=sum(blobs[h] for h in shared_hashes) if blobs else 0,
            low_confidence_sessions=sum(
                1 for session in sessions if session.repo_confidence in {"low", "medium"}
            ),
            without_header_sessions=sum(1 for session in sessions if not session.has_header),
        )

        if unmatched:
            plan.warnings.append(
                f"No sessions matched for: {', '.join(unmatched)}. "
                "Use exact repo labels from `cursorscope analyze`, or try --match suffix."
            )
        if not sessions:
            plan.warnings.append("No sessions matched the requested repositories.")
        if plan.without_header_sessions:
            plan.warnings.append(
                f"{plan.without_header_sessions:,} matched session(s) are not in composer.composerHeaders; "
                "they are often historical and invisible to Cursor's Delete Old Chats."
            )
        if plan.low_confidence_sessions:
            plan.warnings.append(
                f"{plan.low_confidence_sessions:,} matched session(s) have medium/low repo confidence."
            )
        if shared_hashes:
            plan.notes.append(
                f"{human_bytes(plan.agentkv_shared_bytes)} of agentKv blobs are still referenced by "
                "other sessions and will not be removed."
            )
        if scan_agentkv:
            plan.notes.append(
                "agentKv estimates use structural agentKv:blob references found in session payloads."
            )
        plan.notes.append(
            "Attributed bytes include shared agentKv totals per session; exclusive agentKv is the "
            "better reclaim estimate."
        )
        plan.notes.append("This is a dry plan only. Use spike/prune_repos.py --execute to mutate state.vscdb.")
        return plan
    finally:
        conn.close()


def plan_to_dict(plan: RemovePlan) -> dict:
    return asdict(plan)


def print_remove_plan(plan: RemovePlan) -> None:
    print("CursorScope plan-remove (read-only)")
    print(f"source:   {plan.source}")
    print(f"database: {plan.database}")
    print()
    print(f"Repositories requested: {', '.join(plan.repos_requested)}")
    if plan.repos_matched:
        print(f"Repositories matched:   {', '.join(plan.repos_matched)}")
    if plan.repos_unmatched:
        print(f"No match for:           {', '.join(plan.repos_unmatched)}")
    print()
    print(
        f"Sessions: {plan.session_count:,}  "
        f"Attributed: {human_bytes(plan.attributed_bytes)}  "
        f"KV keys: {plan.kv_keys_estimate:,}"
    )
    print(
        f"agentKv attributed: {human_bytes(plan.agentkv_attributed_bytes)}  "
        f"exclusive: {human_bytes(plan.agentkv_exclusive_bytes)}  "
        f"shared (kept): {human_bytes(plan.agentkv_shared_bytes)}"
    )
    print(
        f"Without header index: {plan.without_header_sessions:,}  "
        f"Medium/low confidence: {plan.low_confidence_sessions:,}"
    )
    print()

    if plan.sessions:
        print(f"{'Size':>10}  {'Conf':<6}  {'Hdr':<3}  Session")
        print("-" * 88)
        for session in plan.sessions[:25]:
            label = session.name or session.subtitle or session.composer_id
            hdr = "yes" if session.has_header else "no"
            print(
                f"{human_bytes(session.bytes):>10}  {session.repo_confidence:<6}  {hdr:<3}  "
                f"{label} [{session.repo}]"
            )
        if len(plan.sessions) > 25:
            print(f"{'':>10}           … and {len(plan.sessions) - 25} more")
        print()

    if plan.warnings:
        print("Warnings")
        for warning in plan.warnings:
            print(f"  ! {warning}")
        print()

    if plan.notes:
        print("Notes")
        for note in plan.notes:
            print(f"  - {note}")


def session_key_patterns(composer_id: str) -> list[str]:
    patterns: list[str] = [f"composerData:{composer_id}"]
    patterns.extend(
        f"{prefix}{composer_id}:%" for prefix in SESSION_PREFIXES if prefix != "composerData:"
    )
    return patterns


def exclusive_agentkv_hashes(
    conn: sqlite3.Connection,
    composer_ids: set[str],
) -> tuple[set[str], dict[str, int]]:
    blobs = load_blob_index(conn)
    if not blobs:
        return set(), {}
    composer_refs, _ = scan_structural_references(conn, set(blobs))
    delete_hashes: set[str] = set()
    keep_hashes: set[str] = set()
    for composer_id, hashes in composer_refs.items():
        if composer_id in composer_ids:
            delete_hashes |= hashes
        else:
            keep_hashes |= hashes
    exclusive = delete_hashes - keep_hashes
    return exclusive, blobs
