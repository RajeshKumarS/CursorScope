from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .api import CursorScopeApi

STATIC_DIR = Path(__file__).resolve().parent / "static"
DESKTOP_DIST = Path(__file__).resolve().parents[1] / "desktop" / "dist"


def static_root() -> Path | None:
    if STATIC_DIR.is_dir() and any(STATIC_DIR.iterdir()):
        return STATIC_DIR
    if DESKTOP_DIST.is_dir():
        return DESKTOP_DIST
    return None


def make_handler(api: CursorScopeApi):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CursorScope/0.2"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors()
            self.end_headers()
            self.wfile.write(body)

        def _send_cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._send_cors()
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path.startswith("/api/"):
                return self._route_api_get(path, query)

            root = static_root()
            if root is None:
                self._send_json(
                    404,
                    {
                        "error": "UI not built",
                        "hint": "Run npm run build in desktop/ or use Vite dev server",
                    },
                )
                return

            rel = path.lstrip("/") or "index.html"
            file_path = (root / rel).resolve()
            if not str(file_path).startswith(str(root.resolve())):
                self.send_error(403)
                return
            if file_path.is_dir():
                file_path = file_path / "index.html"
            if not file_path.is_file():
                file_path = root / "index.html"
            if not file_path.is_file():
                self.send_error(404)
                return
            content = file_path.read_bytes()
            mime, _ = mimetypes.guess_type(str(file_path))
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/open":
                try:
                    body = self._read_json()
                    status = api.open_database(
                        src=Path(body["src"]) if body.get("src") else None,
                        snapshot=Path(body["snapshot"]) if body.get("snapshot") else None,
                        cached_snapshot=bool(body.get("cached_snapshot")),
                        ephemeral=bool(body.get("ephemeral")),
                        workspace_dir=(
                            Path(body["workspace_dir"]) if body.get("workspace_dir") else None
                        ),
                        fast=bool(body.get("fast", True)),
                        scan_agentkv=bool(body.get("scan_agentkv", False)),
                    )
                    self._send_json(200, status)
                except Exception as exc:
                    self._send_json(400, {"error": str(exc)})
                return
            if parsed.path == "/api/close":
                api.close_database()
                self._send_json(200, {"closed": True})
                return
            if parsed.path == "/api/agentkv/analyze":
                try:
                    body = self._read_json()
                    use_cache = bool(body.get("use_cache", True))
                    self._send_json(200, api.analyze_agentkv(use_cache=use_cache))
                except RuntimeError as exc:
                    self._send_json(409, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            if parsed.path == "/api/checkpoints":
                try:
                    body = self._read_json()
                    label = str(body.get("label", "")).strip()
                    note = body.get("note")
                    self._send_json(
                        200,
                        api.create_checkpoint_summary(
                            label,
                            note=str(note).strip() if note else None,
                        ),
                    )
                except RuntimeError as exc:
                    self._send_json(409, {"error": str(exc)})
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            self._send_json(404, {"error": "not found"})

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/checkpoints/"):
                ref = unquote(parsed.path.split("/api/checkpoints/", 1)[1])
                try:
                    self._send_json(200, api.remove_checkpoint(ref))
                except RuntimeError as exc:
                    self._send_json(409, {"error": str(exc)})
                except ValueError as exc:
                    self._send_json(404, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            self._send_json(404, {"error": "not found"})

        def _route_api_get(self, path: str, query: dict[str, list[str]]) -> None:
            try:
                if path == "/api/status":
                    self._send_json(200, api.status())
                elif path == "/api/overview":
                    self._send_json(200, api.overview())
                elif path == "/api/repos":
                    self._send_json(200, api.list_repos())
                elif path == "/api/scan/progress":
                    self._send_json(200, api.scan_progress())
                elif path == "/api/sessions":
                    repo = query.get("repo", [None])[0]
                    no_header = query.get("no_header", ["0"])[0] in ("1", "true", "yes")
                    limit_raw = query.get("limit", ["500"])[0]
                    limit = int(limit_raw) if limit_raw else 500
                    self._send_json(
                        200,
                        api.list_sessions(repo=repo, no_header=no_header, limit=limit),
                    )
                elif path.startswith("/api/sessions/"):
                    composer_id = unquote(path.split("/api/sessions/", 1)[1])
                    top = int(query.get("top", ["30"])[0])
                    self._send_json(200, api.get_session(composer_id, top=top))
                elif path == "/api/timeline":
                    repo = query.get("repo", [None])[0]
                    limit_raw = query.get("limit", ["500"])[0]
                    limit = int(limit_raw) if limit_raw else 500
                    self._send_json(200, api.timeline(repo=repo, limit=limit))
                elif path == "/api/growth":
                    save = query.get("save", ["1"])[0] not in ("0", "false", "no")
                    self._send_json(200, api.growth(save=save))
                elif path == "/api/history":
                    limit = int(query.get("limit", ["20"])[0])
                    self._send_json(200, api.scan_history(limit=limit))
                elif path == "/api/checkpoints":
                    limit = int(query.get("limit", ["50"])[0])
                    self._send_json(200, api.list_checkpoint_summaries(limit=limit))
                elif path == "/api/checkpoints/diff":
                    from_ref = query.get("from", [""])[0]
                    to_ref = query.get("to", [""])[0]
                    if not from_ref or not to_ref:
                        self._send_json(400, {"error": "from and to query params required"})
                        return
                    self._send_json(200, api.compare_checkpoints(from_ref, to_ref))
                elif path == "/api/integrity":
                    top = int(query.get("top", ["15"])[0])
                    self._send_json(200, api.integrity(top=top))
                else:
                    self._send_json(404, {"error": "not found"})
            except RuntimeError as exc:
                self._send_json(409, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})

    return Handler


def _ensure_port_available(host: str, port: int) -> None:
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        raise SystemExit(
            f"Port {host}:{port} is already in use. "
            "Stop other `cursorscope serve` processes before starting a new one."
        ) from None
    finally:
        probe.close()


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    auto_open: bool = False,
    api: CursorScopeApi | None = None,
) -> None:
    api = api or CursorScopeApi()
    handler = make_handler(api)
    _ensure_port_available(host, port)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"CursorScope API listening on {url}", flush=True)
    if static_root():
        print(f"Serving UI from {static_root()}", flush=True)
    else:
        print("UI static files not found; API only (use desktop npm run dev)", flush=True)

    if auto_open:
        import webbrowser

        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        api.close_database()
        server.server_close()
