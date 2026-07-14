from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


_REPO_PATH_RE = re.compile(r'"repoPath"\s*:\s*"((?:[^"\\]|\\.)*)"')
_FS_PATH_RE = re.compile(r'"fsPath"\s*:\s*"((?:[^"\\]|\\.)*)"')
_NAME_RE = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')
_SUBTITLE_RE = re.compile(r'"subtitle"\s*:\s*"((?:[^"\\]|\\.)*)"')
_WORKSPACE_ID_RE = re.compile(
    r'"workspaceIdentifier"\s*:\s*\{.*?"id"\s*:\s*"([^"]+)"',
    re.DOTALL,
)
_WORKSPACE_URI_FS_PATH_RE = re.compile(
    r'"workspaceIdentifier"\s*:\s*\{.*?"uri"\s*:\s*\{.*?"fsPath"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)

# Full JSON parse is fine below this; above it we regex-scan a prefix.
_FULL_PARSE_BYTE_LIMIT = 2_000_000
_PARTIAL_SCAN_BYTES = 500_000


def _unescape_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\\\", "\\").replace('\\"', '"')


_PROJECT_MARKERS = (
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)


def infer_git_repo_root(file_path: str) -> str | None:
    path = Path(file_path)
    for parent in path.parents:
        if (parent / ".git").is_dir():
            return str(parent)
    return None


def infer_project_root(file_path: str, *, max_depth: int = 8) -> str | None:
    git_root = infer_git_repo_root(file_path)
    if git_root:
        return git_root

    path = Path(file_path)
    for depth, parent in enumerate(path.parents):
        if depth >= max_depth:
            break
        for marker in _PROJECT_MARKERS:
            if (parent / marker).is_file():
                return str(parent)

    parents = list(path.parents)
    if len(parents) >= 3:
        candidate = str(parents[2])
        if not _is_shallow_path(candidate):
            return candidate
    return None


def _is_shallow_path(path: str, *, min_parts: int = 5) -> bool:
    return len(Path(path).parts) < min_parts


def _longest_common_path(paths: list[str]) -> str | None:
    if not paths:
        return None
    normalized = [str(Path(path)) for path in paths]
    try:
        common = os.path.commonpath(normalized)
    except ValueError:
        return None
    common_path = Path(common)
    if common_path.is_file():
        return str(common_path.parent)
    return common


def infer_repo_from_file_paths(paths: list[str]) -> str | None:
    roots: Counter[str] = Counter()
    for path in paths:
        root = infer_project_root(path)
        if root:
            roots[root] += 1

    if roots:
        deep = [(root, count) for root, count in roots.items() if not _is_shallow_path(root)]
        if deep:
            return sorted(deep, key=lambda item: (-item[1], -len(Path(item[0]).parts)))[0][0]
        common = _longest_common_path(paths)
        if common and not _is_shallow_path(common):
            return common
        return roots.most_common(1)[0][0]

    common = _longest_common_path(paths)
    if common and not _is_shallow_path(common):
        return common
    return None


def _collect_repo_hints_from_obj(
    obj: Any,
) -> tuple[list[str], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    repo_paths: list[str] = []
    fs_paths: list[str] = []
    git_repos: list[dict[str, Any]] = []
    workspace_ids: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            repo_path = node.get("repoPath")
            if isinstance(repo_path, str) and repo_path:
                repo_paths.append(repo_path)
            fs_path = node.get("fsPath")
            if isinstance(fs_path, str) and fs_path:
                fs_paths.append(fs_path)
            tracked = node.get("trackedGitRepos")
            if isinstance(tracked, list):
                git_repos.extend(item for item in tracked if isinstance(item, dict))
            workspace = node.get("workspaceIdentifier")
            if isinstance(workspace, dict) and workspace:
                workspace_ids.append(workspace)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return repo_paths, fs_paths, git_repos, workspace_ids


def _supplement_repo_fields(fields: dict[str, Any], text: str) -> dict[str, Any]:
    if fields.get("trackedGitRepos") or fields.get("workspaceIdentifier"):
        return fields

    repo_paths, fs_paths, git_repos, workspace_ids = _collect_repo_hints_from_obj(fields)
    enriched = dict(fields)

    if git_repos:
        enriched["trackedGitRepos"] = git_repos
    elif repo_paths:
        enriched["trackedGitRepos"] = [{"repoPath": repo_paths[0]}]
    elif fs_paths:
        inferred = infer_repo_from_file_paths(fs_paths)
        if inferred:
            enriched["trackedGitRepos"] = [{"repoPath": inferred}]
            enriched["_inferred_from_fsPath"] = True

    if workspace_ids and not enriched.get("workspaceIdentifier"):
        enriched["workspaceIdentifier"] = workspace_ids[0]

    if enriched.get("trackedGitRepos") or enriched.get("workspaceIdentifier"):
        return enriched

    early = text[:100_000]
    scan = text[:_PARTIAL_SCAN_BYTES]
    if match := _NAME_RE.search(early):
        enriched.setdefault("name", _unescape_json_string(match.group(1)))
    if match := _SUBTITLE_RE.search(early):
        enriched.setdefault("subtitle", _unescape_json_string(match.group(1)))

    repo_paths = [_unescape_json_string(match.group(1)) for match in _REPO_PATH_RE.finditer(scan)]
    if repo_paths:
        enriched["trackedGitRepos"] = [{"repoPath": repo_paths[0]}]
        return enriched

    if match := _WORKSPACE_ID_RE.search(scan):
        enriched["workspaceIdentifier"] = {"id": match.group(1)}
    if match := _WORKSPACE_URI_FS_PATH_RE.search(scan):
        ws = enriched.setdefault("workspaceIdentifier", {})
        ws["uri"] = {"fsPath": _unescape_json_string(match.group(1))}
    if enriched.get("workspaceIdentifier"):
        return enriched

    fs_paths = [_unescape_json_string(match.group(1)) for match in _FS_PATH_RE.finditer(scan)]
    inferred = infer_repo_from_file_paths(fs_paths)
    if inferred:
        enriched["trackedGitRepos"] = [{"repoPath": inferred}]
        enriched["_inferred_from_fsPath"] = True

    return enriched


def extract_composer_fields(text: str) -> dict[str, Any]:
    """Extract composer metadata from full or partial composerData JSON."""
    if not text:
        return {}

    if len(text) <= _FULL_PARSE_BYTE_LIMIT:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return _supplement_repo_fields(obj, text)
        except json.JSONDecodeError:
            pass

    fields: dict[str, Any] = {}
    early = text[:100_000]
    scan = text[:_PARTIAL_SCAN_BYTES]

    if match := _NAME_RE.search(early):
        fields["name"] = _unescape_json_string(match.group(1))
    if match := _SUBTITLE_RE.search(early):
        fields["subtitle"] = _unescape_json_string(match.group(1))

    repo_paths = [_unescape_json_string(match.group(1)) for match in _REPO_PATH_RE.finditer(scan)]
    if repo_paths:
        fields["trackedGitRepos"] = [{"repoPath": repo_paths[0]}]

    if match := _WORKSPACE_ID_RE.search(scan):
        fields["workspaceIdentifier"] = {"id": match.group(1)}
    if match := _WORKSPACE_URI_FS_PATH_RE.search(scan):
        ws = fields.setdefault("workspaceIdentifier", {})
        ws["uri"] = {"fsPath": _unescape_json_string(match.group(1))}

    if "trackedGitRepos" not in fields:
        fs_paths = [_unescape_json_string(match.group(1)) for match in _FS_PATH_RE.finditer(scan)]
        inferred = infer_repo_from_file_paths(fs_paths)
        if inferred:
            fields["trackedGitRepos"] = [{"repoPath": inferred}]
            fields["_inferred_from_fsPath"] = True

    return fields


def load_composer_metadata(
    conn: sqlite3.Connection,
    composer_id: str,
    *,
    partial: bool = True,
) -> dict[str, Any]:
    key = f"composerData:{composer_id}"
    if partial:
        length_row = conn.execute(
            "SELECT length(value) FROM cursorDiskKV WHERE key = ?",
            (key,),
        ).fetchone()
        if not length_row or length_row[0] is None:
            return {}
        value_len = int(length_row[0])
        if value_len <= _FULL_PARSE_BYTE_LIMIT:
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (key,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT substr(value, 1, ?) FROM cursorDiskKV WHERE key = ?",
                (_PARTIAL_SCAN_BYTES, key),
            ).fetchone()
    else:
        row = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (key,),
        ).fetchone()

    if not row or row[0] is None:
        return {}

    raw = row[0]
    text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    return extract_composer_fields(text)


def default_workspace_storage_dir() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Cursor" / "User" / "workspaceStorage"


def file_uri_to_path(uri: str) -> str | None:
    if not uri:
        return None
    if not uri.startswith("file://"):
        return uri
    parsed = urlparse(uri)
    path = unquote(parsed.path or "")
    if not path:
        return None
    # /c:/Users/... on Windows
    if re.match(r"^/[A-Za-z]:/", path):
        return path[1:].replace("/", "\\")
    if os.name == "nt":
        return path.lstrip("/").replace("/", "\\")
    return path


def uri_to_path(uri_obj: Any) -> str | None:
    if isinstance(uri_obj, str):
        if uri_obj.startswith("file://"):
            return file_uri_to_path(uri_obj)
        return uri_obj
    if isinstance(uri_obj, dict):
        if uri_obj.get("fsPath"):
            return str(uri_obj["fsPath"])
        if uri_obj.get("path"):
            p = str(uri_obj["path"])
            if re.match(r"^/[A-Za-z]:/", p):
                return p[1:].replace("/", "\\")
            return p
        if uri_obj.get("external"):
            return uri_to_path(uri_obj["external"])
    return None


class WorkspaceResolver:
    """Resolve Cursor workspace ids via workspaceStorage/<id>/workspace.json."""

    def __init__(self, storage_dir: Path | None = None) -> None:
        self.storage_dir = (storage_dir or default_workspace_storage_dir()).resolve()
        self._cache: dict[str, str | None] = {}

    def resolve(self, workspace_id: str) -> str | None:
        workspace_id = workspace_id.strip()
        if not workspace_id:
            return None
        if workspace_id in self._cache:
            return self._cache[workspace_id]

        path = self.storage_dir / workspace_id / "workspace.json"
        if not path.is_file():
            self._cache[workspace_id] = None
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._cache[workspace_id] = None
            return None

        folder = None
        if isinstance(data, dict):
            for key in ("folder", "workspace"):
                if key in data and isinstance(data[key], str):
                    folder = file_uri_to_path(data[key])
                    if folder:
                        break

        self._cache[workspace_id] = folder
        return folder

    def resolve_label(self, label: str) -> tuple[str, str]:
        """Resolve workspace:<id> labels. Returns (label, confidence)."""
        if not label.startswith("workspace:"):
            return label, "high"
        workspace_id = label[len("workspace:") :]
        folder = self.resolve(workspace_id)
        if folder:
            return folder, "high"
        return label, "medium"


def resolve_repo(
    composer: dict[str, Any],
    resolver: WorkspaceResolver | None = None,
) -> tuple[str, str]:
    """
    Return (repo_label, confidence).

    confidence:
      high   - git path, workspace URI, or workspace.json folder
      medium - workspace id only, or unresolved workspace:<id>
      low    - unknown
    """
    resolver = resolver or WorkspaceResolver()

    git = composer.get("trackedGitRepos") or []
    if isinstance(git, list) and git and isinstance(git[0], dict) and git[0].get("repoPath"):
        return str(git[0]["repoPath"]), "high"

    ws = composer.get("workspaceIdentifier") or {}
    if isinstance(ws, dict):
        uri = ws.get("uri")
        path = uri_to_path(uri)
        if path:
            return path, "high"

        workspace_id = ws.get("id")
        if workspace_id:
            folder = resolver.resolve(str(workspace_id))
            if folder:
                return folder, "high"
            return f"workspace:{workspace_id}", "medium"

    return "(unknown)", "low"


def resolve_repo_from_fallback(
    meta: dict[str, Any],
    resolver: WorkspaceResolver | None = None,
) -> tuple[str, str]:
    """Resolve repo from composerData metadata; confidence capped at medium."""
    repo, confidence = resolve_repo(meta, resolver)
    if repo == "(unknown)":
        return repo, "low"
    if confidence == "high":
        return repo, "medium"
    return repo, confidence
