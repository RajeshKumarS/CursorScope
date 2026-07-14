from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analyze import AnalysisReport, build_report, human_bytes
from .db import SourceFingerprint, default_source_db, source_fingerprint
from .workspace import WorkspaceResolver

SCAN_VERSION = 1
DEFAULT_KEEP_SCANS = 100


def default_history_dir() -> Path:
    return Path.home() / ".cursorscope" / "history"


def default_backup_path(source: Path | None = None) -> Path:
    src = (source or default_source_db()).resolve()
    return src.parent / "state.vscdb.backup"


def backup_cache_path(history_dir: Path | None = None) -> Path:
    return (history_dir or default_history_dir()) / "backup-snapshot.json"


@dataclass
class ScanRecord:
    id: str
    recorded_at: str
    source: str
    source_size: int
    source_mtime_ns: int
    database_file_bytes: int
    cursor_disk_kv_bytes: int
    kv_rows: int
    repos_attributed_bytes: int
    unknown_session_bytes: int
    storage_by_type: dict[str, int]
    unattributed: dict[str, int]
    top_repos: list[dict[str, Any]]
    integrity: dict[str, int | str]
    scan_version: int = SCAN_VERSION
    kind: str = "auto"
    label: str | None = None
    note: str | None = None

    @property
    def fingerprint(self) -> SourceFingerprint:
        return SourceFingerprint(size=self.source_size, mtime_ns=self.source_mtime_ns)


@dataclass
class DeltaRow:
    label: str
    previous: int
    current: int
    delta: int


@dataclass
class GrowthReport:
    current: ScanRecord
    previous: ScanRecord | None = None
    backup: ScanRecord | None = None
    delta_file_bytes: int = 0
    delta_kv_bytes: int = 0
    delta_by_type: list[DeltaRow] = field(default_factory=list)
    delta_top_repos: list[DeltaRow] = field(default_factory=list)
    headline: str | None = None
    backup_headline: str | None = None
    comparison_title: str | None = None
    notes: list[str] = field(default_factory=list)
    saved: bool = False


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_id(recorded_at: str) -> str:
    return recorded_at.replace(":", "").replace("-", "")


def snapshot_from_report(report: AnalysisReport) -> ScanRecord:
    source_path = Path(report.source)
    try:
        fp = source_fingerprint(source_path)
    except OSError:
        fp = SourceFingerprint(size=0, mtime_ns=0)

    recorded_at = _utc_now()
    return ScanRecord(
        id=_record_id(recorded_at),
        recorded_at=recorded_at,
        source=str(source_path.resolve()),
        source_size=fp.size,
        source_mtime_ns=fp.mtime_ns,
        database_file_bytes=report.pages.file_bytes,
        cursor_disk_kv_bytes=report.cursor_disk_kv_bytes,
        kv_rows=report.kv_rows,
        repos_attributed_bytes=report.repos_attributed_bytes,
        unknown_session_bytes=report.unknown_session_bytes,
        storage_by_type={row.prefix: row.bytes for row in report.storage_by_type},
        unattributed=dict(report.unattributed),
        top_repos=[
            {
                "repo": row.repo,
                "bytes": row.bytes,
                "session_count": row.session_count,
            }
            for row in report.top_repos[:25]
        ],
        integrity=dict(report.integrity),
    )


def record_to_dict(record: ScanRecord) -> dict[str, Any]:
    return asdict(record)


def record_from_dict(data: dict[str, Any]) -> ScanRecord:
    return ScanRecord(
        id=str(data["id"]),
        recorded_at=str(data["recorded_at"]),
        source=str(data["source"]),
        source_size=int(data["source_size"]),
        source_mtime_ns=int(data["source_mtime_ns"]),
        database_file_bytes=int(data["database_file_bytes"]),
        cursor_disk_kv_bytes=int(data["cursor_disk_kv_bytes"]),
        kv_rows=int(data["kv_rows"]),
        repos_attributed_bytes=int(data["repos_attributed_bytes"]),
        unknown_session_bytes=int(data["unknown_session_bytes"]),
        storage_by_type={str(k): int(v) for k, v in (data.get("storage_by_type") or {}).items()},
        unattributed={str(k): int(v) for k, v in (data.get("unattributed") or {}).items()},
        top_repos=list(data.get("top_repos") or []),
        integrity=dict(data.get("integrity") or {}),
        scan_version=int(data.get("scan_version") or SCAN_VERSION),
        kind=str(data.get("kind") or "auto"),
        label=data.get("label"),
        note=data.get("note"),
    )


def _scan_path(history_dir: Path, record_id: str) -> Path:
    return history_dir / f"{record_id}.json"


def save_scan(
    record: ScanRecord,
    *,
    history_dir: Path | None = None,
    keep: int = DEFAULT_KEEP_SCANS,
) -> Path:
    record.kind = "auto"
    history_dir = (history_dir or default_history_dir()).resolve()
    history_dir.mkdir(parents=True, exist_ok=True)
    path = _scan_path(history_dir, record.id)
    path.write_text(json.dumps(record_to_dict(record), indent=2), encoding="utf-8")
    latest = history_dir / "latest.json"
    latest.write_text(json.dumps(record_to_dict(record), indent=2), encoding="utf-8")
    prune_old_scans(history_dir=history_dir, keep=keep, preserve={record.id})
    return path


def save_checkpoint(
    record: ScanRecord,
    *,
    history_dir: Path | None = None,
) -> Path:
    record.kind = "checkpoint"
    history_dir = (history_dir or default_history_dir()).resolve()
    history_dir.mkdir(parents=True, exist_ok=True)
    path = _scan_path(history_dir, record.id)
    path.write_text(json.dumps(record_to_dict(record), indent=2), encoding="utf-8")
    return path


def prune_old_scans(
    *,
    history_dir: Path | None = None,
    keep: int = DEFAULT_KEEP_SCANS,
    preserve: set[str] | None = None,
) -> list[Path]:
    history_dir = (history_dir or default_history_dir()).resolve()
    if not history_dir.is_dir():
        return []
    preserve = preserve or set()
    preserve.add("latest")
    preserve.add("backup-snapshot")
    auto_files: list[Path] = []
    for path in sorted(history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.stem in preserve:
            continue
        record = load_record(path)
        if record is None or record.kind != "auto":
            continue
        auto_files.append(path)
    removed: list[Path] = []
    for path in auto_files[keep:]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            pass
    return removed


def load_record(path: Path) -> ScanRecord | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return record_from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def load_latest_scan(
    source: str | Path | None = None,
    *,
    history_dir: Path | None = None,
    exclude_id: str | None = None,
) -> ScanRecord | None:
    history_dir = (history_dir or default_history_dir()).resolve()
    latest = load_record(history_dir / "latest.json")
    source_key = str(Path(source).resolve()) if source else None

    if latest and exclude_id and latest.id == exclude_id:
        latest = None
    if latest and source_key and latest.source != source_key:
        latest = None

    scans = list_scans(source=source, history_dir=history_dir, limit=DEFAULT_KEEP_SCANS)
    for record in scans:
        if exclude_id and record.id == exclude_id:
            continue
        return record
    return latest


def list_scans(
    *,
    source: str | Path | None = None,
    history_dir: Path | None = None,
    limit: int = 20,
    kind: str | None = "auto",
) -> list[ScanRecord]:
    history_dir = (history_dir or default_history_dir()).resolve()
    if not history_dir.is_dir():
        return []
    source_key = str(Path(source).resolve()) if source else None
    records: list[ScanRecord] = []
    for path in sorted(history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.stem in {"latest", "backup-snapshot"}:
            continue
        record = load_record(path)
        if record is None:
            continue
        if kind and record.kind != kind:
            continue
        if source_key and record.source != source_key:
            continue
        records.append(record)
        if len(records) >= limit:
            break
    return records


def list_checkpoints(
    *,
    source: str | Path | None = None,
    history_dir: Path | None = None,
    limit: int = 50,
) -> list[ScanRecord]:
    return list_scans(source=source, history_dir=history_dir, limit=limit, kind="checkpoint")


def _same_snapshot(a: ScanRecord, b: ScanRecord) -> bool:
    return (
        a.source == b.source
        and a.source_size == b.source_size
        and a.source_mtime_ns == b.source_mtime_ns
        and a.database_file_bytes == b.database_file_bytes
        and a.cursor_disk_kv_bytes == b.cursor_disk_kv_bytes
        and a.kv_rows == b.kv_rows
    )


def _delta_rows(current: ScanRecord, baseline: ScanRecord) -> list[DeltaRow]:
    labels = set(current.storage_by_type) | set(baseline.storage_by_type)
    rows = [
        DeltaRow(
            label=label,
            previous=baseline.storage_by_type.get(label, 0),
            current=current.storage_by_type.get(label, 0),
            delta=current.storage_by_type.get(label, 0) - baseline.storage_by_type.get(label, 0),
        )
        for label in labels
    ]
    rows.sort(key=lambda row: abs(row.delta), reverse=True)
    return [row for row in rows if row.delta != 0]


def _delta_top_repos(current: ScanRecord, baseline: ScanRecord) -> list[DeltaRow]:
    current_map = {row["repo"]: int(row["bytes"]) for row in current.top_repos}
    baseline_map = {row["repo"]: int(row["bytes"]) for row in baseline.top_repos}
    labels = set(current_map) | set(baseline_map)
    rows = [
        DeltaRow(
            label=label,
            previous=baseline_map.get(label, 0),
            current=current_map.get(label, 0),
            delta=current_map.get(label, 0) - baseline_map.get(label, 0),
        )
        for label in labels
    ]
    rows.sort(key=lambda row: abs(row.delta), reverse=True)
    return [row for row in rows if row.delta != 0]


def _format_signed_delta(delta: int) -> str:
    sign = "+" if delta > 0 else ""
    return f"{sign}{human_bytes(delta)}"


def _headline(
    *,
    since_label: str,
    delta_file_bytes: int,
    delta_kv_bytes: int,
    delta_by_type: list[DeltaRow],
) -> str | None:
    if delta_file_bytes == 0 and delta_kv_bytes == 0 and not delta_by_type:
        return None
    parts: list[str] = []
    if delta_file_bytes:
        parts.append(f"{_format_signed_delta(delta_file_bytes)} file")
    if delta_kv_bytes and delta_kv_bytes != delta_file_bytes:
        parts.append(f"{_format_signed_delta(delta_kv_bytes)} KV payloads")
    top = next((row for row in delta_by_type if row.delta > 0), None)
    driver = f", mostly {top.label}" if top and top.delta > 0 else ""
    if parts:
        return f"{since_label}: {', '.join(parts)}{driver}"
    if top:
        return f"{since_label}: {_format_signed_delta(top.delta)} in {top.label}"
    return f"{since_label}: no net change in tracked categories"


def compare_records(current: ScanRecord, baseline: ScanRecord, *, since_label: str) -> GrowthReport:
    delta_by_type = _delta_rows(current, baseline)
    delta_top_repos = _delta_top_repos(current, baseline)
    delta_file = current.database_file_bytes - baseline.database_file_bytes
    delta_kv = current.cursor_disk_kv_bytes - baseline.cursor_disk_kv_bytes
    headline = _headline(
        since_label=since_label,
        delta_file_bytes=delta_file,
        delta_kv_bytes=delta_kv,
        delta_by_type=delta_by_type,
    )
    return GrowthReport(
        current=current,
        previous=baseline,
        delta_file_bytes=delta_file,
        delta_kv_bytes=delta_kv,
        delta_by_type=delta_by_type,
        delta_top_repos=delta_top_repos,
        headline=headline,
    )


def snapshot_from_db(
    db_path: Path,
    *,
    source: Path,
    resolver: WorkspaceResolver | None = None,
    fast: bool = True,
) -> ScanRecord:
    report = build_report(
        db_path,
        source=source,
        top_n=10,
        top_n_repos=25,
        resolver=resolver,
        scan_agentkv=False,
        progress=False,
        use_cache=True,
        fast=fast,
    )
    return snapshot_from_report(report)


def load_or_build_backup_record(
    source: Path,
    *,
    history_dir: Path | None = None,
    resolver: WorkspaceResolver | None = None,
    allow_build: bool = True,
) -> ScanRecord | None:
    backup = default_backup_path(source)
    if not backup.is_file():
        return None
    try:
        fp = source_fingerprint(backup)
    except OSError:
        return None

    cache = backup_cache_path(history_dir)
    cached = load_record(cache)
    if cached and cached.source_size == fp.size and cached.source_mtime_ns == fp.mtime_ns:
        return cached
    if not allow_build:
        return None

    record = snapshot_from_db(backup, source=backup, resolver=resolver, fast=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(record_to_dict(record), indent=2), encoding="utf-8")
    return record


def build_growth_report(
    report: AnalysisReport,
    *,
    history_dir: Path | None = None,
    save: bool = True,
    include_backup: bool = True,
    backup_cached_only: bool = False,
    resolver: WorkspaceResolver | None = None,
) -> GrowthReport:
    history_dir = (history_dir or default_history_dir()).resolve()
    current = snapshot_from_report(report)
    notes: list[str] = []

    previous = load_latest_scan(current.source, history_dir=history_dir)
    growth = GrowthReport(current=current, notes=notes)

    if previous and _same_snapshot(current, previous):
        notes.append("Database unchanged since the last recorded scan; no new history entry written.")
        save = False
    elif previous:
        compared = compare_records(current, previous, since_label="Since last scan")
        growth.previous = compared.previous
        growth.delta_file_bytes = compared.delta_file_bytes
        growth.delta_kv_bytes = compared.delta_kv_bytes
        growth.delta_by_type = compared.delta_by_type
        growth.headline = compared.headline
        if previous.recorded_at:
            notes.append(f"Previous scan: {previous.recorded_at} UTC.")
    else:
        notes.append("No prior scan history for this database; baseline recorded.")

    if include_backup:
        backup = load_or_build_backup_record(
            Path(current.source),
            history_dir=history_dir,
            resolver=resolver,
            allow_build=not backup_cached_only,
        )
        if backup and not _same_snapshot(current, backup):
            growth.backup = backup
            backup_cmp = compare_records(current, backup, since_label="Since Cursor backup")
            growth.backup_headline = backup_cmp.headline
            notes.append(f"Cursor backup file: {default_backup_path(Path(current.source))}")
        elif backup:
            notes.append("Cursor backup matches current database size; no backup delta.")
        elif backup_cached_only and default_backup_path(Path(current.source)).is_file():
            notes.append(
                "Cursor backup present; run `cursorscope growth` once to cache backup comparison."
            )

    if save:
        save_scan(current, history_dir=history_dir)
        growth.saved = True

    return growth


def growth_to_dict(report: GrowthReport) -> dict[str, Any]:
    return {
        "headline": report.headline,
        "backup_headline": report.backup_headline,
        "comparison_title": report.comparison_title,
        "delta_file_bytes": report.delta_file_bytes,
        "delta_kv_bytes": report.delta_kv_bytes,
        "delta_by_type": [asdict(row) for row in report.delta_by_type[:15]],
        "delta_top_repos": [asdict(row) for row in report.delta_top_repos[:15]],
        "previous": record_to_dict(report.previous) if report.previous else None,
        "backup": record_to_dict(report.backup) if report.backup else None,
        "current": record_to_dict(report.current),
        "saved": report.saved,
        "notes": report.notes,
    }


def print_growth_report(report: GrowthReport) -> None:
    if report.headline:
        print(report.headline)
    if report.backup_headline:
        print(report.backup_headline)
    if report.delta_by_type:
        print()
        print("Storage delta by type")
        print(f"{'Type':<42} {'Previous':>10}  {'Current':>10}  {'Delta':>10}")
        print("-" * 78)
        for row in report.delta_by_type[:12]:
            print(
                f"{row.label:<42} {human_bytes(row.previous):>10}  "
                f"{human_bytes(row.current):>10}  {_format_signed_delta(row.delta):>10}"
            )
    if report.delta_top_repos:
        print()
        print("Top repository deltas")
        print(f"{'Repository':<42} {'Previous':>10}  {'Current':>10}  {'Delta':>10}")
        print("-" * 78)
        for row in report.delta_top_repos[:12]:
            label = row.label if len(row.label) <= 42 else row.label[:39] + "..."
            print(
                f"{label:<42} {human_bytes(row.previous):>10}  "
                f"{human_bytes(row.current):>10}  {_format_signed_delta(row.delta):>10}"
            )
    for note in report.notes:
        print(f"Note: {note}")
    if report.saved:
        print(f"Note: Scan saved to {default_history_dir()}")


def print_history_list(records: list[ScanRecord]) -> None:
    if not records:
        print("No scan history recorded yet.")
        print(f"History directory: {default_history_dir()}")
        return
    print("CursorScope scan history")
    print(f"{'Recorded (UTC)':<22} {'Kind':<12} {'Label':<20} {'File':>10}  {'KV':>10}")
    print("-" * 80)
    for record in records:
        label = record.label or "—"
        print(
            f"{record.recorded_at:<22} "
            f"{record.kind:<12} "
            f"{label[:20]:<20} "
            f"{human_bytes(record.database_file_bytes):>10}  "
            f"{human_bytes(record.cursor_disk_kv_bytes):>10}"
        )


def _checkpoint_name(record: ScanRecord) -> str:
    return record.label or record.id


def find_checkpoint(
    ref: str,
    *,
    source: str | Path | None = None,
    history_dir: Path | None = None,
) -> ScanRecord:
    ref = ref.strip()
    if not ref:
        raise ValueError("checkpoint reference required")
    checkpoints = list_checkpoints(source=source, history_dir=history_dir, limit=500)
    for record in checkpoints:
        if record.id == ref or record.id.startswith(ref):
            return record
    matches = [
        record
        for record in checkpoints
        if record.label and record.label.lower() == ref.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"multiple checkpoints named {ref!r}; use id instead")
    raise ValueError(f"checkpoint not found: {ref!r}")


def create_checkpoint(
    report: AnalysisReport,
    label: str,
    *,
    note: str | None = None,
    history_dir: Path | None = None,
) -> ScanRecord:
    label = label.strip()
    if not label:
        raise ValueError("checkpoint label is required")
    record = snapshot_from_report(report)
    record.label = label
    record.note = note.strip() if note else None
    save_checkpoint(record, history_dir=history_dir)
    return record


def diff_checkpoints(
    from_ref: str,
    to_ref: str,
    *,
    source: str | Path | None = None,
    history_dir: Path | None = None,
) -> GrowthReport:
    baseline = find_checkpoint(from_ref, source=source, history_dir=history_dir)
    current = find_checkpoint(to_ref, source=source, history_dir=history_dir)
    if baseline.source != current.source:
        raise ValueError("checkpoints are from different databases")
    from_name = _checkpoint_name(baseline)
    to_name = _checkpoint_name(current)
    report = compare_records(
        current,
        baseline,
        since_label=f"Between '{from_name}' and '{to_name}'",
    )
    report.comparison_title = f"Checkpoint compare: {from_name} → {to_name}"
    report.notes.append(f"From: {baseline.recorded_at} UTC ({from_name})")
    report.notes.append(f"To: {current.recorded_at} UTC ({to_name})")
    return report


def delete_checkpoint(
    ref: str,
    *,
    source: str | Path | None = None,
    history_dir: Path | None = None,
) -> bool:
    record = find_checkpoint(ref, source=source, history_dir=history_dir)
    path = _scan_path(history_dir or default_history_dir(), record.id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def print_checkpoint_list(records: list[ScanRecord]) -> None:
    if not records:
        print("No checkpoints saved yet.")
        print(f"Create one with: cursorscope checkpoint create \"before cleanup\"")
        return
    print("CursorScope checkpoints")
    print(f"{'Label':<24} {'Recorded (UTC)':<22} {'File':>10}  {'ID':<16}")
    print("-" * 78)
    for record in records:
        label = record.label or "(unnamed)"
        print(
            f"{label[:24]:<24} "
            f"{record.recorded_at:<22} "
            f"{human_bytes(record.database_file_bytes):>10}  "
            f"{record.id[:16]}"
        )
