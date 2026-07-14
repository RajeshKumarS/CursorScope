from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .agentkv import build_agentkv_report
from .analyze import AnalysisReport, build_report
from .db import ResolvedDb, default_source_db, open_ro
from .integrity import build_integrity_report, integrity_report_to_dict
from .session import build_session_report, session_report_to_dict
from .sessions import build_sessions_report, sessions_report_to_dict
from .timeline import build_timeline_report, timeline_report_to_dict
from .history import (
    build_growth_report,
    create_checkpoint,
    delete_checkpoint,
    diff_checkpoints,
    growth_to_dict,
    list_checkpoints,
    list_scans,
    record_to_dict,
)
from .workspace import WorkspaceResolver, default_workspace_storage_dir


def _analysis_to_dict(report: AnalysisReport) -> dict[str, Any]:
    data = asdict(report)
    data["pages"] = asdict(report.pages)
    if report.agentkv:
        data["agentkv"] = asdict(report.agentkv)
    return data


class CursorScopeApi:
    """In-process API for CLI, HTTP server, and desktop UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._resolved: ResolvedDb | None = None
        self._workspace_dir: Path | None = None
        self._fast = True
        self._scan_agentkv = False
        self._agentkv_cache: dict[str, Any] | None = None
        self._scan_progress: dict[str, Any] = {"active": False}

    def scan_progress(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._scan_progress)

    def _clear_scan_progress(self) -> None:
        with self._lock:
            self._scan_progress = {"active": False}

    def _begin_scan_progress(self) -> None:
        with self._lock:
            self._scan_progress = {
                "active": True,
                "label": "Starting scan…",
                "current": 0,
                "total": 0,
                "percent": 0.0,
                "cached": False,
                "finishing": False,
                "eta_sec": None,
            }

    def _report_scan_progress(
        self,
        current: int,
        total: int,
        label: str,
        *,
        cached: bool = False,
        eta_sec: float | None = None,
        finishing: bool = False,
    ) -> None:
        with self._lock:
            if not self._scan_progress.get("active"):
                return
            if finishing:
                if total > 0:
                    percent = 90.0 + 10.0 * current / total
                else:
                    percent = 95.0
                self._scan_progress.update(
                    {
                        "label": label,
                        "current": current,
                        "total": total,
                        "percent": min(100.0, percent),
                        "cached": False,
                        "finishing": True,
                        "eta_sec": None,
                    }
                )
                return
            percent = 90.0 if cached else (min(90.0, 100.0 * current / total) if total else 0.0)
            self._scan_progress.update(
                {
                    "label": "Using cached scan" if cached else label,
                    "current": current,
                    "total": total,
                    "percent": percent,
                    "cached": cached,
                    "finishing": False,
                    "eta_sec": eta_sec,
                }
            )

    def _report_scan_phase(self, label: str) -> None:
        self._report_scan_progress(0, 0, label, finishing=True)

    @property
    def is_open(self) -> bool:
        return self._resolved is not None

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self._resolved:
                return {"open": False}
            return {
                "open": True,
                "source": str(self._resolved.source),
                "database": str(self._resolved.db_path),
                "live": self._resolved.db_path == self._resolved.source,
                "fast": self._fast,
                "scan_agentkv": self._scan_agentkv,
            }

    def open_database(
        self,
        *,
        src: Path | None = None,
        snapshot: Path | None = None,
        cached_snapshot: bool = False,
        ephemeral: bool = False,
        workspace_dir: Path | None = None,
        fast: bool = True,
        scan_agentkv: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._workspace_dir = workspace_dir
            self._fast = fast
            self._scan_agentkv = scan_agentkv
            self._resolved = resolve_live_db_path(
                src=src,
                snapshot=snapshot,
                cached_snapshot=cached_snapshot,
                ephemeral=ephemeral,
            )
            return self.status()

    def close_database(self) -> None:
        with self._lock:
            if self._resolved:
                from .db import release_resolved_db

                release_resolved_db(self._resolved)
            self._resolved = None
            self._agentkv_cache = None

    def _resolver(self) -> WorkspaceResolver:
        return WorkspaceResolver(self._workspace_dir or default_workspace_storage_dir())

    def _require_open(self) -> ResolvedDb:
        if not self._resolved:
            raise RuntimeError("database not open; call open first")
        return self._resolved

    def overview(self, *, record_history: bool = True, track_progress: bool = False) -> dict[str, Any]:
        try:
            if track_progress:
                self._begin_scan_progress()
            report = self._build_overview_report(track_progress=track_progress)
            data = _analysis_to_dict(report)
            if record_history:
                if track_progress:
                    self._report_scan_phase("Recording scan history…")
                growth = build_growth_report(
                    report,
                    save=True,
                    backup_cached_only=True,
                    resolver=self._resolver(),
                )
                data["growth"] = growth_to_dict(growth)
            return data
        finally:
            if track_progress:
                self._clear_scan_progress()

    def growth(self, *, save: bool = True) -> dict[str, Any]:
        data = self.overview(record_history=save, track_progress=False)
        return dict(data.get("growth") or {})

    def scan_history(self, *, limit: int = 20) -> dict[str, Any]:
        resolved = self._require_open()
        records = list_scans(source=resolved.source, limit=limit, kind="auto")
        return {
            "source": str(resolved.source),
            "scans": [record_to_dict(record) for record in records],
        }

    def _build_overview_report(self, *, track_progress: bool = False) -> AnalysisReport:
        resolved = self._require_open()
        return build_report(
            resolved.db_path,
            source=resolved.source,
            top_n=25,
            top_n_repos=500,
            resolver=self._resolver(),
            scan_agentkv=self._scan_agentkv,
            progress=False,
            use_cache=True,
            fast=self._fast,
            on_progress=self._report_scan_progress if track_progress else None,
        )

    def list_checkpoint_summaries(self, *, limit: int = 50) -> dict[str, Any]:
        resolved = self._require_open()
        records = list_checkpoints(source=resolved.source, limit=limit)
        return {
            "source": str(resolved.source),
            "checkpoints": [record_to_dict(record) for record in records],
        }

    def create_checkpoint_summary(
        self,
        label: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        report = self._build_overview_report()
        record = create_checkpoint(report, label, note=note)
        return record_to_dict(record)

    def compare_checkpoints(self, from_ref: str, to_ref: str) -> dict[str, Any]:
        resolved = self._require_open()
        growth = diff_checkpoints(
            from_ref,
            to_ref,
            source=resolved.source,
        )
        return growth_to_dict(growth)

    def remove_checkpoint(self, ref: str) -> dict[str, Any]:
        resolved = self._require_open()
        deleted = delete_checkpoint(ref, source=resolved.source)
        return {"deleted": deleted, "ref": ref}

    def list_repos(self) -> dict[str, Any]:
        data = self.overview(record_history=True, track_progress=True)
        repos = data.get("top_repos", [])
        pages = data.get("pages", {})
        unattributed = data.get("unattributed", {})
        repos_attributed = int(data.get("repos_attributed_bytes") or 0)
        unknown_session = int(data.get("unknown_session_bytes") or 0)
        kv_bytes = int(data.get("cursor_disk_kv_bytes") or 0)
        file_bytes = int((pages or {}).get("file_bytes") or 0)
        other_storage = max(0, kv_bytes - repos_attributed - unknown_session)
        breakdown = data.get("other_storage_breakdown", [])
        integrity = data.get("integrity", {})
        return {
            "repos": repos,
            "database_file_bytes": file_bytes,
            "cursor_disk_kv_bytes": kv_bytes,
            "repos_attributed_bytes": repos_attributed,
            "unknown_session_bytes": unknown_session,
            "table_total_bytes": repos_attributed + unknown_session,
            "other_storage_bytes": other_storage,
            "other_storage_breakdown": breakdown,
            "unattributed": unattributed,
            "integrity": integrity,
            "growth": data.get("growth"),
        }

    def list_sessions(
        self,
        *,
        repo: str | None = None,
        no_header: bool = False,
        limit: int | None = 500,
    ) -> dict[str, Any]:
        resolved = self._require_open()
        report = build_sessions_report(
            resolved.db_path,
            source=resolved.source,
            top_n=limit,
            no_header=no_header,
            repo_filter=repo,
            resolver=self._resolver(),
        )
        return sessions_report_to_dict(report)

    def get_session(self, composer_id: str, *, top: int = 30) -> dict[str, Any]:
        resolved = self._require_open()
        report = build_session_report(
            resolved.db_path,
            composer_id,
            source=resolved.source,
            top_n=top,
            resolver=self._resolver(),
            scan_agentkv=self._scan_agentkv and not self._fast,
        )
        return session_report_to_dict(report)

    def integrity(self, *, top: int = 15) -> dict[str, Any]:
        resolved = self._require_open()
        report = build_integrity_report(
            resolved.db_path,
            source=resolved.source,
            top_n=top,
            resolver=self._resolver(),
            scan_agentkv=self._scan_agentkv and not self._fast,
            progress=False,
            use_cache=True,
            fast=self._fast,
        )
        return integrity_report_to_dict(report)

    def analyze_agentkv(self, *, use_cache: bool = True) -> dict[str, Any]:
        with self._lock:
            if use_cache and self._agentkv_cache is not None:
                return dict(self._agentkv_cache)

            resolved = self._require_open()
            import time

            conn = open_ro(resolved.db_path)
            try:
                start = time.perf_counter()
                report = build_agentkv_report(conn, progress=False)
                elapsed = time.perf_counter() - start
            finally:
                conn.close()

            total = report.total_bytes or 1
            payload: dict[str, Any] = {
                **asdict(report),
                "elapsed_sec": round(elapsed, 1),
                "referenced_ratio": report.referenced_bytes_unique / total,
                "unreferenced_ratio": report.unreferenced_bytes / total,
                "top_attributed_sessions": [
                    {"composer_id": composer_id, "bytes": size}
                    for composer_id, size in sorted(
                        report.attributed_by_composer.items(),
                        key=lambda item: -item[1],
                    )[:20]
                ],
            }
            self._agentkv_cache = payload
            return dict(payload)

    def timeline(
        self,
        *,
        repo: str | None = None,
        limit: int | None = 500,
    ) -> dict[str, Any]:
        resolved = self._require_open()
        report = build_timeline_report(
            resolved.db_path,
            source=resolved.source,
            resolver=self._resolver(),
            repo_filter=repo,
            limit=limit,
            use_cache=True,
        )
        return timeline_report_to_dict(report)


def resolve_live_db_path(
    *,
    src: Path | None = None,
    snapshot: Path | None = None,
    cached_snapshot: bool = False,
    ephemeral: bool = False,
) -> ResolvedDb:
    """Resolve DB path for API/desktop without argparse."""
    from .db import (
        default_snapshot_path,
        find_reusable_snapshot,
        prune_old_snapshots,
        snapshot_copy,
    )
    import tempfile

    source = (src or default_source_db()).resolve()
    if snapshot:
        path = snapshot.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Snapshot not found: {path}")
        return ResolvedDb(db_path=path, source=source)

    if not cached_snapshot and not ephemeral:
        if not source.exists():
            raise FileNotFoundError(f"Source DB not found: {source}")
        return ResolvedDb(db_path=source, source=source)

    if not source.exists():
        raise FileNotFoundError(f"Source DB not found: {source}")

    snapshot_dir = Path.home() / ".cursorscope" / "snapshots"
    if ephemeral:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="cursorscope-", dir=snapshot_dir))
        db_path = temp_dir / "state.vscdb"
        snapshot_copy(source, db_path)
        return ResolvedDb(db_path=db_path, source=source, ephemeral=True, temp_dir=temp_dir)

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    reusable = find_reusable_snapshot(snapshot_dir, source)
    if reusable is not None:
        return ResolvedDb(db_path=reusable, source=source)

    db_path = default_snapshot_path(snapshot_dir)
    snapshot_copy(source, db_path, source=source)
    prune_old_snapshots(snapshot_dir, keep=db_path)
    return ResolvedDb(db_path=db_path, source=source)
