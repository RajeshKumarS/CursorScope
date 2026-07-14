# CursorScope desktop UI

Browser UI for `state.vscdb`: **Repositories**, **Timeline**, and drill-down to **Sessions** and **Session detail**.

Talks to the same Python backend as the CLI via a local HTTP API (`cursorscope serve`).

## Prerequisites

- Python 3.9+ with CursorScope installed: `pip install -e ..`
- Node.js 18+
- For native app builds: [Rust](https://rustup.rs/) and Tauri prerequisites

## Development (browser)

Terminal 1 — API server:

```bash
python -m cursorscope serve
```

Terminal 2 — Vite dev server (proxies `/api` to port 8765):

```bash
cd desktop
npm install
npm run dev
```

Open http://127.0.0.1:5173

By default the UI reads `state.vscdb` **live** with no disk copy. Pass `ephemeral: true` or `cached_snapshot: true` to `openDatabase()` if you need a point-in-time copy.

## Production static build

```bash
cd desktop
npm install
npm run build
```

Output lands in `cursorscope/static/`. Then:

```bash
python -m cursorscope serve --open
```

## Tauri native shell

```bash
cd desktop
npm install
npm run tauri:dev    # dev
npm run tauri:build  # installer (requires Rust)
```

The packaged app expects `python -m cursorscope` on `PATH`.

## API (selected)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/open` | Open DB (live, ephemeral, or cached snapshot) |
| `POST` | `/api/close` | Release DB |
| `GET` | `/api/status` | Connection status |
| `GET` | `/api/repos` | Repository list, storage totals, growth summary |
| `GET` | `/api/scan/progress` | Scan progress while `/api/repos` is running |
| `GET` | `/api/sessions?repo=` | Sessions for a repo |
| `GET` | `/api/sessions/:id` | Session detail |
| `GET` | `/api/timeline` | Timeline by month / repo / sessions |
| `GET` | `/api/growth` | Growth vs last scan |
| `GET` | `/api/history` | Saved scan summaries |
| `GET` | `/api/checkpoints` | Labeled checkpoints |
| `POST` | `/api/checkpoints` | Create checkpoint |
| `GET` | `/api/checkpoints/diff` | Compare two checkpoints |
| `GET` | `/api/integrity` | Integrity report |
| `GET` | `/api/overview` | Full analyze report |
