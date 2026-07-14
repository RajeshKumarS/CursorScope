import {
  analyzeAgentKv,
  createCheckpoint,
  deleteCheckpoint,
  diffCheckpoints,
  formatBytes,
  getSession,
  getScanProgress,
  getTimeline,
  listCheckpoints,
  listRepos,
  listSessions,
  num,
  openDatabase,
  type AgentKvAnalysis,
  type CheckpointRecord,
  type GrowthResponse,
  type OtherStorageRow,
  type ReposResponse,
  type ScanProgress,
  type SessionDetail,
  type SessionsResponse,
  type TimelineResponse,
} from "./api";

type Route =
  | { view: "repos" }
  | { view: "timeline" }
  | { view: "sessions"; repo: string }
  | { view: "session"; repo: string; composerId: string };

const viewEl = document.getElementById("view")!;
const statusLine = document.getElementById("status-line")!;
const breadcrumbs = document.getElementById("breadcrumbs")!;
const mainNav = document.getElementById("main-nav")!;

let route: Route = { view: "repos" };
let reposCache: ReposResponse | null = null;
let timelineCache: TimelineResponse | null = null;
const sessionsCache = new Map<string, SessionsResponse>();

function setStatus(text: string) {
  statusLine.textContent = text;
}

function setLoading(message = "Loading…") {
  viewEl.innerHTML = `<div class="loading">${message}</div>`;
}

function setLoadingWithProgress(message = "Loading…") {
  viewEl.innerHTML = `
    <div class="loading loading-progress">
      <div class="loading-message">${message}</div>
      <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div class="progress-fill"></div>
      </div>
      <div class="loading-detail muted"></div>
    </div>
  `;
}

function updateLoadingProgress(progress: ScanProgress) {
  const root = viewEl.querySelector(".loading-progress");
  if (!root) {
    return;
  }
  const messageEl = root.querySelector(".loading-message") as HTMLElement | null;
  const fill = root.querySelector(".progress-fill") as HTMLElement | null;
  const track = root.querySelector(".progress-track") as HTMLElement | null;
  const detail = root.querySelector(".loading-detail") as HTMLElement | null;
  if (!messageEl || !fill || !track || !detail) {
    return;
  }

  messageEl.textContent = progress.label || "Scanning…";
  const percent = Math.round(progress.percent ?? 0);
  fill.style.width = `${percent}%`;
  track.setAttribute("aria-valuenow", String(percent));
  track.classList.toggle("progress-indeterminate", Boolean(progress.finishing) && !(progress.total && progress.total > 0));

  if (progress.cached) {
    detail.textContent = "Loaded from cache";
  } else if (progress.finishing && progress.total && progress.total > 0) {
    detail.textContent = `${(progress.current ?? 0).toLocaleString()} / ${progress.total.toLocaleString()} sessions`;
  } else if (progress.finishing) {
    detail.textContent = "Almost done…";
  } else if (progress.total && progress.total > 0) {
    const eta =
      progress.eta_sec != null && progress.eta_sec > 0
        ? ` · ~${Math.ceil(progress.eta_sec)}s remaining`
        : "";
    detail.textContent = `${(progress.current ?? 0).toLocaleString()} / ${progress.total.toLocaleString()} rows${eta}`;
  } else {
    detail.textContent = "";
  }
}

async function pollScanProgress(signal: AbortSignal): Promise<void> {
  while (!signal.aborted) {
    try {
      const progress = await getScanProgress();
      updateLoadingProgress(progress);
    } catch {
      // Ignore transient poll errors while the scan request is in flight.
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

function setError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  viewEl.innerHTML = `<div class="error">${message}</div>`;
}

function createBackButton(label: string, onBack: () => void): HTMLElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "back-btn";
  button.textContent = `← ${label}`;
  button.onclick = onBack;
  return button;
}

function createCollapsibleSection(
  title: string,
  options: {
    defaultOpen?: boolean;
    secondary?: boolean;
    subtitle?: string;
  } = {},
): { section: HTMLElement; body: HTMLElement } {
  const open = options.defaultOpen ?? false;
  const section = document.createElement("section");
  section.className = `panel collapsible${options.secondary ? " panel-secondary" : ""}`;

  const header = document.createElement("button");
  header.type = "button";
  header.className = "panel-header collapsible-header";
  header.setAttribute("aria-expanded", String(open));
  header.innerHTML = `
    <span class="collapsible-title">
      <strong>${escapeHtml(title)}</strong>
      ${options.subtitle ? `<span class="muted collapsible-subtitle">${escapeHtml(options.subtitle)}</span>` : ""}
    </span>
    <span class="collapsible-chevron" aria-hidden="true">${open ? "▾" : "▸"}</span>
  `;

  const body = document.createElement("div");
  body.className = "panel-body collapsible-body";
  body.hidden = !open;

  header.onclick = () => {
    const nextOpen = body.hidden;
    body.hidden = !nextOpen;
    header.setAttribute("aria-expanded", String(nextOpen));
    const chevron = header.querySelector(".collapsible-chevron");
    if (chevron) {
      chevron.textContent = nextOpen ? "▾" : "▸";
    }
  };

  section.append(header, body);
  return { section, body };
}

function growthSubtitle(growth: GrowthResponse | null | undefined): string | undefined {
  if (!growth) {
    return undefined;
  }
  if (growth.headline) {
    return growth.headline;
  }
  if (growth.previous) {
    return "No change since last scan";
  }
  return "Baseline recorded";
}

const DISK_SPACE_TIPS = [
  {
    title: "Developer: GC Agent KV Blobs",
    body:
      "Run from the Command Palette in Cursor. Removes unreferenced agent KV blobs from state.vscdb.",
  },
  {
    title: "Restart Cursor periodically",
    body:
      "If you keep Cursor open for days, the state.vscdb-wal file can grow large. Quitting and reopening Cursor lets SQLite reclaim that space.",
  },
] as const;

function createDiskSpaceTips(): HTMLElement {
  const host = document.createElement("div");
  host.className = "tips-host";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "tips-trigger";
  button.setAttribute("aria-label", "Tips to save disk space");
  button.setAttribute("aria-expanded", "false");
  button.title = "Tips to save disk space";
  button.innerHTML = `
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.75"></circle>
      <path d="M12 11v5" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"></path>
      <circle cx="12" cy="8" r="1.1" fill="currentColor"></circle>
    </svg>
  `;

  const popover = document.createElement("div");
  popover.className = "tips-popover";
  popover.hidden = true;
  popover.setAttribute("role", "dialog");
  popover.setAttribute("aria-label", "Tips to save disk space");

  const listItems = DISK_SPACE_TIPS.map(
    (tip) => `
      <li>
        <strong>${escapeHtml(tip.title)}</strong>
        <p>${escapeHtml(tip.body)}</p>
      </li>
    `,
  ).join("");

  popover.innerHTML = `
    <div class="tips-popover-card">
      <div class="tips-popover-header">
        <strong>Save disk space in Cursor</strong>
        <button type="button" class="tips-close" aria-label="Close">×</button>
      </div>
      <ul class="tips-list">${listItems}</ul>
      <p class="muted tips-footnote">
        Open the Command Palette (<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd> or <kbd>⌘</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd>)
        and search for the command above.
      </p>
    </div>
  `;

  const closeButton = popover.querySelector(".tips-close") as HTMLButtonElement;

  let outsideListener: ((event: MouseEvent) => void) | null = null;

  const close = () => {
    popover.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (outsideListener) {
      document.removeEventListener("click", outsideListener);
      outsideListener = null;
    }
  };

  const open = () => {
    popover.hidden = false;
    button.setAttribute("aria-expanded", "true");
    outsideListener = (event: MouseEvent) => {
      if (!host.contains(event.target as Node)) {
        close();
      }
    };
    setTimeout(() => {
      if (outsideListener) {
        document.addEventListener("click", outsideListener);
      }
    }, 0);
  };

  button.onclick = (event) => {
    event.stopPropagation();
    if (popover.hidden) {
      open();
    } else {
      close();
    }
  };

  closeButton.onclick = (event) => {
    event.stopPropagation();
    close();
  };

  host.onkeydown = (event) => {
    if (event.key === "Escape" && !popover.hidden) {
      event.stopPropagation();
      close();
    }
  };

  host.append(button, popover);
  return host;
}

function createSummaryStrip(data: ReposResponse): HTMLElement {
  const strip = document.createElement("div");
  strip.className = "summary-strip";

  const content = document.createElement("div");
  content.className = "summary-strip-content";
  const parts = [
    `<span><strong>${formatBytes(data.database_file_bytes)}</strong> file</span>`,
    `<span>${formatBytes(data.cursor_disk_kv_bytes)} KV payloads</span>`,
    `<span>${formatBytes(data.other_storage_bytes)} unassigned</span>`,
    `<span>${data.repos.length} repos</span>`,
  ];
  const hint = growthSubtitle(data.growth);
  if (hint) {
    parts.push(`<span class="summary-strip-growth">${escapeHtml(hint)}</span>`);
  }
  content.innerHTML = parts.join('<span class="summary-strip-sep">·</span>');
  strip.append(content, createDiskSpaceTips());
  return strip;
}

function createTabBar(tabs: { id: string; label: string }[]): {
  bar: HTMLElement;
  panels: HTMLElement;
  show: (id: string) => void;
  getPanel: (id: string) => HTMLElement;
} {
  const bar = document.createElement("div");
  bar.className = "tab-bar";
  bar.setAttribute("role", "tablist");

  const panels = document.createElement("div");
  panels.className = "tab-panels";

  const panelMap = new Map<string, HTMLElement>();

  function show(id: string) {
    for (const tab of tabs) {
      const button = bar.querySelector(`[data-tab-id="${tab.id}"]`) as HTMLButtonElement | null;
      const panel = panelMap.get(tab.id);
      if (!button || !panel) {
        continue;
      }
      const active = tab.id === id;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      panel.hidden = !active;
    }
  }

  for (const tab of tabs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tab-btn";
    button.dataset.tabId = tab.id;
    button.textContent = tab.label;
    button.setAttribute("role", "tab");
    button.onclick = () => show(tab.id);
    bar.appendChild(button);

    const panel = document.createElement("div");
    panel.className = "tab-panel panel";
    panel.dataset.tabPanel = tab.id;
    panel.hidden = true;
    panelMap.set(tab.id, panel);
    panels.appendChild(panel);
  }

  return {
    bar,
    panels,
    show(id: string) {
      show(id);
    },
    getPanel(id: string) {
      return panelMap.get(id)!;
    },
  };
}

function confidenceClass(confidence: string): string {
  if (confidence === "high") return "conf-high";
  if (confidence === "medium") return "conf-medium";
  return "conf-low";
}

function renderMainNav() {
  mainNav.innerHTML = "";
  const reposBtn = document.createElement("button");
  reposBtn.type = "button";
  reposBtn.className = `nav-tab${route.view === "repos" ? " active" : ""}`;
  reposBtn.textContent = "Repositories";
  reposBtn.onclick = () => goRepos();

  const timelineBtn = document.createElement("button");
  timelineBtn.type = "button";
  timelineBtn.className = `nav-tab${route.view === "timeline" ? " active" : ""}`;
  timelineBtn.textContent = "Timeline";
  timelineBtn.onclick = () => goTimeline();

  mainNav.append(reposBtn, timelineBtn);
}

function renderBreadcrumbs() {
  renderMainNav();
  breadcrumbs.innerHTML = "";

  if (route.view === "sessions" || route.view === "session") {
    const repo = route.repo;
    breadcrumbs.appendChild(document.createElement("span")).textContent = "›";
    const repoCrumb = document.createElement("button");
    repoCrumb.textContent = shortenPath(repo);
    repoCrumb.onclick = () => goSessions(repo);
    breadcrumbs.appendChild(repoCrumb);
  }

  if (route.view === "session") {
    const composerId = route.composerId;
    breadcrumbs.appendChild(document.createElement("span")).textContent = "›";
    const sessionCrumb = document.createElement("span");
    sessionCrumb.textContent = composerId.slice(0, 8);
    breadcrumbs.appendChild(sessionCrumb);
  }
}

function shortenPath(path: string): string {
  if (path.length <= 48) return path;
  const parts = path.replace(/\\/g, "/").split("/");
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

function goRepos() {
  route = { view: "repos" };
  renderBreadcrumbs();
  void renderRepos();
}

function goTimeline() {
  route = { view: "timeline" };
  renderBreadcrumbs();
  void renderTimeline();
}

function goSessions(repo: string) {
  route = { view: "sessions", repo };
  renderBreadcrumbs();
  void renderSessions(repo);
}

function navigate(next: Route) {
  route = next;
  renderBreadcrumbs();
  void render();
}

function isAgentKvRow(row: OtherStorageRow): boolean {
  return row.label.toLowerCase().includes("agentkv");
}

function renderAgentKvResults(report: AgentKvAnalysis): HTMLElement {
  const root = document.createElement("div");
  root.className = "agentkv-results";

  const pct = (ratio: number) => `${(ratio * 100).toFixed(1)}%`;

  const summary = document.createElement("ul");
  summary.className = "meta-list";
  summary.innerHTML = `
    <li><span>Total blobs</span><span class="size">${report.total_blobs.toLocaleString()} (${formatBytes(report.total_bytes)})</span></li>
    <li><span>Structurally linked to sessions</span><span class="size">${report.referenced_blobs.toLocaleString()} blobs · ${formatBytes(report.referenced_bytes_unique)} (${pct(report.referenced_ratio)})</span></li>
    <li><span>Unreferenced in session payloads</span><span class="size">${report.unreferenced_blobs.toLocaleString()} blobs · ${formatBytes(report.unreferenced_bytes)} (${pct(report.unreferenced_ratio)})</span></li>
    <li><span>Referrer rows with blob hashes</span><span class="size">${report.structural_reference_rows.toLocaleString()}</span></li>
    <li><span>composer.content overlap</span><span class="size">${report.overlap_blobs.toLocaleString()} hashes · ${formatBytes(report.overlap_bytes)}</span></li>
    <li><span>Scan time</span><span class="size">${report.elapsed_sec}s</span></li>
  `;
  root.appendChild(summary);

  if (report.notes.length) {
    const notes = document.createElement("ul");
    notes.className = "agentkv-notes muted";
    for (const note of report.notes) {
      const li = document.createElement("li");
      li.textContent = note;
      notes.appendChild(li);
    }
    root.appendChild(notes);
  }

  if (report.top_attributed_sessions.length) {
    const heading = document.createElement("p");
    heading.className = "agentkv-subheading";
    heading.textContent = "Top sessions with linked agentKv bytes";
    root.appendChild(heading);

    const table = document.createElement("table");
    table.className = "breakdown-table";
    table.innerHTML = `
      <thead><tr><th>Composer ID</th><th>Linked agentKv</th></tr></thead>
      <tbody></tbody>
    `;
    const tbody = table.querySelector("tbody")!;
    for (const row of report.top_attributed_sessions) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${escapeHtml(row.composer_id)}</td>
        <td class="size">${formatBytes(row.bytes)}</td>
      `;
      tbody.appendChild(tr);
    }
    root.appendChild(table);
  } else {
    const empty = document.createElement("p");
    empty.className = "muted agentkv-empty";
    empty.textContent =
      "No structural agentKv:blob references were found in session payloads. Blobs remain unattributed to sessions/repos with current decoding.";
    root.appendChild(empty);
  }

  return root;
}

async function runAgentKvAnalysis(
  detailRow: HTMLTableRowElement,
  detailCell: HTMLTableCellElement,
  button: HTMLButtonElement,
) {
  button.disabled = true;
  button.textContent = "Scanning…";
  detailRow.hidden = false;
  detailCell.innerHTML = `<p class="muted agentkv-scanning">Scanning session payloads for agentKv:blob references…</p>`;

  try {
    const report = await analyzeAgentKv();
    detailCell.innerHTML = "";
    detailCell.appendChild(renderAgentKvResults(report));
    button.textContent = "Refresh analysis";
  } catch (error) {
    detailCell.innerHTML = `<p class="error-inline">${escapeHtml(error instanceof Error ? error.message : String(error))}</p>`;
    button.textContent = "Retry analysis";
  } finally {
    button.disabled = false;
  }
}

function buildOtherStoragePanel(data: ReposResponse): HTMLElement {
  const panel = document.createElement("section");
  panel.className = "panel other-storage-panel";
  panel.id = "other-storage-panel";
  panel.hidden = true;

  const orphanSessions = num(data.integrity.sessions_without_headers);
  const intro = document.createElement("p");
  intro.className = "other-storage-intro muted";
  intro.textContent =
    "KV storage not attributed to a repository/session. This is on-disk SQLite data in state.vscdb—not a live memory leak." +
    (orphanSessions > 0
      ? ` ${orphanSessions} sessions lack a composerHeaders entry (often historical).`
      : "");

  const table = document.createElement("table");
  table.className = "breakdown-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Category</th>
        <th>Keys</th>
        <th>Size</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody")!;
  for (const row of data.other_storage_breakdown) {
    const tr = document.createElement("tr");
    const agentKv = isAgentKvRow(row);
    const td = document.createElement("td");
    td.innerHTML = `
      <div>${escapeHtml(row.label)}</div>
      <div class="muted breakdown-desc">${escapeHtml(row.description)}</div>
    `;
    if (agentKv) {
      const hint = document.createElement("p");
      hint.className = "muted agentkv-slow-hint";
      hint.textContent =
        "Deep analysis scans session payloads for blob references (often 30s–3min on large databases).";
      td.appendChild(hint);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "analyze-btn";
      button.textContent = "Analyze deeper…";
      td.appendChild(button);

      const detailRow = document.createElement("tr");
      detailRow.className = "agentkv-detail-row";
      detailRow.hidden = true;
      const detailCell = document.createElement("td");
      detailCell.colSpan = 3;
      detailCell.className = "agentkv-detail-cell";
      detailRow.appendChild(detailCell);
      button.onclick = () => void runAgentKvAnalysis(detailRow, detailCell, button);

      tr.append(
        td,
        Object.assign(document.createElement("td"), {
          className: "size",
          textContent: row.count > 0 ? row.count.toLocaleString() : "—",
        }),
        Object.assign(document.createElement("td"), {
          className: "size",
          textContent: formatBytes(row.bytes),
        }),
      );
      tbody.appendChild(tr);
      tbody.appendChild(detailRow);
      continue;
    }

    tr.innerHTML = `
      <td>
        <div>${escapeHtml(row.label)}</div>
        <div class="muted breakdown-desc">${escapeHtml(row.description)}</div>
      </td>
      <td class="size">${row.count > 0 ? row.count.toLocaleString() : "—"}</td>
      <td class="size">${formatBytes(row.bytes)}</td>
    `;
    tbody.appendChild(tr);
  }

  const tfoot = document.createElement("tfoot");
  tfoot.innerHTML = `
    <tr class="other-storage-total">
      <td colspan="2"><strong>Total unassigned KV</strong></td>
      <td class="size"><strong>${formatBytes(data.other_storage_bytes)}</strong></td>
    </tr>
  `;
  table.appendChild(tfoot);

  panel.append(intro, table);
  return panel;
}

function formatSignedDelta(delta: number): string {
  const sign = delta > 0 ? "+" : "";
  return `${sign}${formatBytes(delta)}`;
}

function buildGrowthPanel(
  growth: GrowthResponse | null | undefined,
  title = "Since last scan",
  embedded = false,
): HTMLElement | null {
  if (!growth) {
    return null;
  }

  const panel = document.createElement(embedded ? "div" : "section");
  panel.className = embedded ? "growth-embed" : "panel growth-panel";

  if (embedded) {
    const embedTitle = document.createElement("div");
    embedTitle.className = "growth-embed-title";
    embedTitle.textContent = title;
    panel.appendChild(embedTitle);
  } else {
    const header = document.createElement("div");
    header.className = "panel-header";
    header.innerHTML = `<strong>${escapeHtml(title)}</strong>`;
    panel.appendChild(header);
  }

  const body = document.createElement("div");
  body.className = "panel-body";
  panel.appendChild(body);

  if (growth.headline) {
    const primary = document.createElement("p");
    primary.className = "growth-headline";
    primary.textContent = growth.headline;
    body.appendChild(primary);
  } else if (growth.previous) {
    const steady = document.createElement("p");
    steady.className = "growth-headline muted";
    steady.textContent = "No change since the last recorded scan.";
    body.appendChild(steady);
  } else {
    const baseline = document.createElement("p");
    baseline.className = "growth-headline muted";
    baseline.textContent = "First scan recorded — open again later to see growth.";
    body.appendChild(baseline);
  }

  if (growth.backup_headline) {
    const backup = document.createElement("p");
    backup.className = "growth-backup muted";
    backup.textContent = growth.backup_headline;
    body.appendChild(backup);
  }

  if (growth.delta_by_type.length) {
    const table = document.createElement("table");
    table.className = "growth-table";
    table.innerHTML = `
      <thead>
        <tr>
          <th>Type</th>
          <th>Previous</th>
          <th>Current</th>
          <th>Delta</th>
        </tr>
      </thead>
      <tbody></tbody>
    `;
    const tbody = table.querySelector("tbody")!;
    for (const row of growth.delta_by_type.slice(0, 8)) {
      const tr = document.createElement("tr");
      const deltaClass = row.delta > 0 ? "delta-up" : row.delta < 0 ? "delta-down" : "";
      tr.innerHTML = `
        <td>${escapeHtml(row.label)}</td>
        <td class="size">${formatBytes(row.previous)}</td>
        <td class="size">${formatBytes(row.current)}</td>
        <td class="size ${deltaClass}">${formatSignedDelta(row.delta)}</td>
      `;
      tbody.appendChild(tr);
    }
    body.appendChild(table);
  }

  const repoDeltas = growth.delta_top_repos ?? [];
  if (repoDeltas.length) {
    const heading = document.createElement("p");
    heading.className = "agentkv-subheading";
    heading.textContent = "Top repository deltas";
    body.appendChild(heading);

    const table = document.createElement("table");
    table.className = "growth-table";
    table.innerHTML = `
      <thead>
        <tr>
          <th>Repository</th>
          <th>Previous</th>
          <th>Current</th>
          <th>Delta</th>
        </tr>
      </thead>
      <tbody></tbody>
    `;
    const tbody = table.querySelector("tbody")!;
    for (const row of repoDeltas.slice(0, 8)) {
      const tr = document.createElement("tr");
      const deltaClass = row.delta > 0 ? "delta-up" : row.delta < 0 ? "delta-down" : "";
      tr.innerHTML = `
        <td>${escapeHtml(row.label)}</td>
        <td class="size">${formatBytes(row.previous)}</td>
        <td class="size">${formatBytes(row.current)}</td>
        <td class="size ${deltaClass}">${formatSignedDelta(row.delta)}</td>
      `;
      tbody.appendChild(tr);
    }
    body.appendChild(table);
  }

  if (growth.notes.length) {
    const notes = document.createElement("ul");
    notes.className = "agentkv-notes muted";
    for (const note of growth.notes) {
      const li = document.createElement("li");
      li.textContent = note;
      notes.appendChild(li);
    }
    body.appendChild(notes);
  }

  return panel;
}

function checkpointOptionLabel(record: CheckpointRecord): string {
  const label = record.label || record.id;
  return `${label} (${record.recorded_at.slice(0, 16).replace("T", " ")})`;
}

async function mountCheckpointContent(body: HTMLElement) {
  body.innerHTML = `<p class="muted">Loading checkpoints…</p>`;

  let checkpoints: CheckpointRecord[] = [];
  try {
    ({ checkpoints } = await listCheckpoints());
  } catch (error) {
    body.innerHTML = `<p class="error-inline">${escapeHtml(String(error))}</p>`;
    return;
  }

  body.innerHTML = "";
  body.className = "checkpoint-body";

  const heading = document.createElement("p");
  heading.className = "section-label";
  heading.textContent = "Named checkpoints";
  body.appendChild(heading);

  const intro = document.createElement("p");
  intro.className = "muted checkpoint-intro";
  intro.textContent =
    "Save a labeled storage summary (~KB on disk). Compare any two checkpoints without copying state.vscdb.";
  body.appendChild(intro);

  const createRow = document.createElement("div");
  createRow.className = "checkpoint-create";
  const labelInput = document.createElement("input");
  labelInput.type = "text";
  labelInput.placeholder = "Checkpoint label, e.g. before cleanup";
  labelInput.className = "checkpoint-input";
  const createBtn = document.createElement("button");
  createBtn.type = "button";
  createBtn.className = "primary";
  createBtn.textContent = "Create checkpoint";
  createRow.append(labelInput, createBtn);
  body.appendChild(createRow);

  const compareRow = document.createElement("div");
  compareRow.className = "checkpoint-compare";
  const fromLabel = document.createElement("span");
  fromLabel.className = "checkpoint-field-label";
  fromLabel.textContent = "From";
  const fromSelect = document.createElement("select");
  const toLabel = document.createElement("span");
  toLabel.className = "checkpoint-field-label";
  toLabel.textContent = "To";
  const toSelect = document.createElement("select");
  fromSelect.className = "checkpoint-select";
  toSelect.className = "checkpoint-select";
  const compareBtn = document.createElement("button");
  compareBtn.type = "button";
  compareBtn.className = "primary";
  compareBtn.textContent = "Compare";
  compareBtn.disabled = checkpoints.length < 2;
  compareRow.append(fromLabel, fromSelect, toLabel, toSelect, compareBtn);
  body.appendChild(compareRow);

  const resultHost = document.createElement("div");
  resultHost.className = "checkpoint-result";
  body.appendChild(resultHost);

  const listHost = document.createElement("div");
  listHost.className = "checkpoint-list";
  body.appendChild(listHost);

  function refillSelects(items: CheckpointRecord[]) {
    fromSelect.innerHTML = "";
    toSelect.innerHTML = "";
    for (const record of items) {
      const option = document.createElement("option");
      option.value = record.id;
      option.textContent = checkpointOptionLabel(record);
      fromSelect.appendChild(option.cloneNode(true) as HTMLOptionElement);
      toSelect.appendChild(option);
    }
    if (items.length >= 2) {
      toSelect.selectedIndex = 0;
      fromSelect.selectedIndex = 1;
    }
    compareBtn.disabled = items.length < 2;
  }

  function renderList(items: CheckpointRecord[], showAll: boolean) {
    const limit = showAll ? items.length : 5;
    const visible = items.slice(0, limit);
    if (!items.length) {
      listHost.innerHTML = `<p class="muted">No checkpoints yet.</p>`;
      return;
    }
    const table = document.createElement("table");
    table.className = "checkpoint-table";
    table.innerHTML = `
      <thead>
        <tr>
          <th>Label</th>
          <th>Recorded (UTC)</th>
          <th>File size</th>
          <th></th>
        </tr>
      </thead>
      <tbody></tbody>
    `;
    const tbody = table.querySelector("tbody")!;
    for (const record of visible) {
      const tr = document.createElement("tr");
      const label = record.label || "(unnamed)";
      tr.innerHTML = `
        <td>${escapeHtml(label)}</td>
        <td>${escapeHtml(record.recorded_at)}</td>
        <td class="size">${formatBytes(record.database_file_bytes)}</td>
        <td></td>
      `;
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "link-btn";
      deleteBtn.textContent = "Delete";
      deleteBtn.onclick = () => void (async () => {
        deleteBtn.disabled = true;
        try {
          await deleteCheckpoint(record.id);
          checkpoints = checkpoints.filter((item) => item.id !== record.id);
          refillSelects(checkpoints);
          renderList(checkpoints, false);
          resultHost.innerHTML = "";
        } catch (error) {
          alert(error instanceof Error ? error.message : String(error));
          deleteBtn.disabled = false;
        }
      })();
      tr.lastElementChild!.appendChild(deleteBtn);
      tbody.appendChild(tr);
    }
    listHost.innerHTML = "";
    listHost.appendChild(table);
    if (items.length > 5) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "link-btn checkpoint-show-all";
      toggle.textContent = showAll ? "Show fewer" : `Show all ${items.length} checkpoints`;
      toggle.onclick = () => renderList(items, !showAll);
      listHost.appendChild(toggle);
    }
  }

  refillSelects(checkpoints);
  renderList(checkpoints, false);

  createBtn.onclick = () => void (async () => {
    const label = labelInput.value.trim();
    if (!label) {
      labelInput.focus();
      return;
    }
    createBtn.disabled = true;
    createBtn.textContent = "Saving…";
    try {
      const record = await createCheckpoint(label);
      checkpoints = [record, ...checkpoints];
      labelInput.value = "";
      refillSelects(checkpoints);
      renderList(checkpoints, false);
    } catch (error) {
      alert(error instanceof Error ? error.message : String(error));
    } finally {
      createBtn.disabled = false;
      createBtn.textContent = "Create checkpoint";
    }
  })();

  compareBtn.onclick = () => void (async () => {
    const fromRef = fromSelect.value;
    const toRef = toSelect.value;
    if (!fromRef || !toRef || fromRef === toRef) {
      return;
    }
    compareBtn.disabled = true;
    resultHost.innerHTML = `<p class="muted">Comparing…</p>`;
    try {
      const diff = await diffCheckpoints(fromRef, toRef);
      resultHost.innerHTML = "";
      const panel = buildGrowthPanel(
        diff,
        diff.comparison_title || "Checkpoint comparison",
        true,
      );
      if (panel) {
        resultHost.appendChild(panel);
      }
    } catch (error) {
      resultHost.innerHTML = `<p class="error">${escapeHtml(error instanceof Error ? error.message : String(error))}</p>`;
    } finally {
      compareBtn.disabled = checkpoints.length < 2;
    }
  })();
}

function paintRepos(data: ReposResponse) {
  const rows = [...data.repos];
  const tableBytes =
    num(data.table_total_bytes) ||
    rows.reduce((sum, repo) => sum + num(repo.bytes), 0) + num(data.unknown_session_bytes);
  const gapBytes = num(data.database_file_bytes) - num(data.cursor_disk_kv_bytes);

  const root = document.createElement("div");
  root.className = "repos-page";

  root.appendChild(createSummaryStrip(data));

  const wrapper = document.createElement("div");
  wrapper.className = "panel panel-primary";
  wrapper.innerHTML = `
    <div class="panel-header">
      <strong>Repositories</strong>
      <span class="muted">${rows.length} attributed</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Repository</th>
          <th>Sessions</th>
          <th>Confidence</th>
          <th>Size</th>
        </tr>
      </thead>
      <tbody></tbody>
      <tfoot>
        <tr class="total-row">
          <td colspan="3"><strong>Total</strong></td>
          <td class="size"><strong>${formatBytes(tableBytes)}</strong></td>
        </tr>
      </tfoot>
    </table>
  `;
  const tbody = wrapper.querySelector("tbody")!;
  for (const repo of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(repo.repo)}</td>
      <td>${repo.session_count}</td>
      <td class="${confidenceClass(repo.confidence)}">${repo.confidence}</td>
      <td class="size">${formatBytes(repo.bytes)}</td>
    `;
    tr.onclick = () => navigate({ view: "sessions", repo: repo.repo });
    tbody.appendChild(tr);
  }
  const unknownRow = document.createElement("tr");
  unknownRow.innerHTML = `
    <td>(unknown)</td>
    <td>—</td>
    <td class="conf-low">low</td>
    <td class="size">${formatBytes(data.unknown_session_bytes)}</td>
  `;
  unknownRow.onclick = () => navigate({ view: "sessions", repo: "(unknown)" });
  tbody.appendChild(unknownRow);
  root.appendChild(wrapper);

  const changes = createCollapsibleSection("Changes", {
    defaultOpen: false,
    secondary: true,
    subtitle: growthSubtitle(data.growth),
  });
  const growthEmbed = buildGrowthPanel(data.growth, "Since last scan", true);
  if (growthEmbed) {
    changes.body.appendChild(growthEmbed);
  }
  const checkpointHost = document.createElement("div");
  checkpointHost.className = "checkpoint-section";
  changes.body.appendChild(checkpointHost);
  void mountCheckpointContent(checkpointHost);
  root.appendChild(changes.section);

  const storage = createCollapsibleSection("Storage details", {
    defaultOpen: false,
    secondary: true,
    subtitle: `${formatBytes(data.database_file_bytes)} file · ${formatBytes(data.other_storage_bytes)} unassigned`,
  });
  const summaryList = document.createElement("ul");
  summaryList.className = "meta-list summary-list";
  summaryList.innerHTML = `
    <li><span>state.vscdb file size</span><span class="size">${formatBytes(data.database_file_bytes)}</span></li>
    <li><span>cursorDiskKV payloads</span><span class="size">${formatBytes(data.cursor_disk_kv_bytes)}</span></li>
    <li><span>Repositories total (table)</span><span class="size">${formatBytes(data.table_total_bytes)}</span></li>
    <li class="drilldown-row" id="other-storage-toggle" role="button" tabindex="0">
      <span>Other / unassigned KV <span class="drilldown-hint">▸ details</span></span>
      <span class="size">${formatBytes(data.other_storage_bytes)}</span>
    </li>
    <li><span>SQLite overhead &amp; ItemTable</span><span class="size">${formatBytes(gapBytes)}</span></li>
  `;
  storage.body.appendChild(summaryList);

  const otherPanel = buildOtherStoragePanel(data);
  storage.body.appendChild(otherPanel);
  const toggle = summaryList.querySelector("#other-storage-toggle")!;
  const togglePanel = () => {
    const open = otherPanel.hidden;
    otherPanel.hidden = !open;
    toggle.classList.toggle("open", open);
    const hint = toggle.querySelector(".drilldown-hint");
    if (hint) {
      hint.textContent = open ? "▾ details" : "▸ details";
    }
  };
  toggle.addEventListener("click", togglePanel);
  root.appendChild(storage.section);

  viewEl.innerHTML = "";
  viewEl.appendChild(root);
}

async function renderRepos() {
  if (reposCache) {
    paintRepos(reposCache);
    return;
  }
  setLoadingWithProgress("Scanning repositories…");
  const pollController = new AbortController();
  const pollTask = pollScanProgress(pollController.signal);
  try {
    reposCache = await listRepos();
    paintRepos(reposCache);
  } catch (error) {
    setError(error);
  } finally {
    pollController.abort();
    await pollTask;
  }
}

function paintSessions(repo: string, data: SessionsResponse) {
  const root = document.createElement("div");
  root.className = "sessions-page";
  root.appendChild(createBackButton("All repositories", goRepos));

  const wrapper = document.createElement("div");
  wrapper.className = "panel";
  wrapper.innerHTML = `
    <div class="panel-header">
      <div>
        <strong>Sessions</strong>
        <div class="muted">${escapeHtml(repo)}</div>
      </div>
      <span class="muted">${data.total_matching} total</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Confidence</th>
          <th>Header</th>
          <th>Size</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  `;
  const tbody = wrapper.querySelector("tbody")!;
  for (const session of data.sessions) {
    const tr = document.createElement("tr");
    const label = session.name || session.subtitle || session.composer_id;
    tr.innerHTML = `
      <td>
        <div>${escapeHtml(label)}</div>
        <div class="muted">${escapeHtml(session.composer_id)}</div>
      </td>
      <td class="${confidenceClass(session.repo_confidence)}">${session.repo_confidence}</td>
      <td>${session.has_header ? "yes" : "no"}</td>
      <td class="size">${formatBytes(session.bytes)}</td>
    `;
    tr.onclick = () =>
      navigate({ view: "session", repo, composerId: session.composer_id });
    tbody.appendChild(tr);
  }
  root.appendChild(wrapper);
  viewEl.innerHTML = "";
  viewEl.appendChild(root);
}

async function renderSessions(repo: string) {
  const cached = sessionsCache.get(repo);
  if (cached) {
    paintSessions(repo, cached);
    return;
  }
  setLoading("Loading sessions…");
  try {
    const data = await listSessions(repo);
    sessionsCache.set(repo, data);
    paintSessions(repo, data);
  } catch (error) {
    setError(error);
  }
}

function paintSession(repo: string, composerId: string, detail: SessionDetail) {
  const root = document.createElement("div");
  root.className = "session-page";
  root.appendChild(
    createBackButton(shortenPath(repo), () => goSessions(repo)),
  );
  root.appendChild(renderSessionDetail(detail));
  viewEl.innerHTML = "";
  viewEl.appendChild(root);
}

async function renderSession(repo: string, composerId: string) {
  setLoading("Loading session detail…");
  try {
    const detail = await getSession(composerId);
    if (!detail.found) {
      setError(`Session not found: ${composerId}`);
      return;
    }
    paintSession(repo, composerId, detail);
  } catch (error) {
    setError(error);
  }
}

function renderSessionDetail(detail: SessionDetail): HTMLElement {
  const root = document.createElement("div");
  root.className = "detail-grid";

  const meta = document.createElement("section");
  meta.className = "panel";
  meta.innerHTML = `
    <div class="panel-header"><strong>${escapeHtml(detail.name || detail.composer_id)}</strong></div>
    <ul class="meta-list">
      <li><span>Composer ID</span><span>${escapeHtml(detail.composer_id)}</span></li>
      <li><span>Repository</span><span>${escapeHtml(detail.repo || "(unknown)")}</span></li>
      <li><span>Confidence</span><span class="${confidenceClass(detail.repo_confidence)}">${detail.repo_confidence}</span></li>
      <li><span>Total size</span><span class="size">${formatBytes(detail.total_bytes)}</span></li>
    </ul>
  `;

  const breakdown = document.createElement("section");
  breakdown.className = "panel";
  breakdown.innerHTML = `<div class="panel-header"><strong>Storage breakdown</strong></div>`;
  const breakdownList = document.createElement("ul");
  breakdownList.className = "meta-list";
  for (const [kind, size] of Object.entries(detail.storage_breakdown).sort((a, b) => b[1] - a[1])) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(kind)}</span><span class="size">${formatBytes(size)}</span>`;
    breakdownList.appendChild(li);
  }
  breakdown.appendChild(breakdownList);

  const files = document.createElement("section");
  files.className = "panel";
  files.innerHTML = `<div class="panel-header"><strong>Files (${detail.files.length})</strong></div>`;
  const fileList = document.createElement("ul");
  fileList.className = "file-list";
  for (const file of detail.files.slice(0, 40)) {
    const li = document.createElement("li");
    li.className = "file-row";
    li.innerHTML = `
      <span class="chip file-chip">${escapeHtml(file.source)}</span>
      <span class="file-path">${escapeHtml(file.path)}</span>
    `;
    fileList.appendChild(li);
  }
  files.appendChild(fileList);

  root.append(meta, breakdown, files);
  return root;
}

function paintTimeline(data: TimelineResponse) {
  const root = document.createElement("div");
  root.className = "timeline-page";

  const strip = document.createElement("div");
  strip.className = "summary-strip";
  strip.innerHTML = [
    `<span><strong>${data.sessions_total.toLocaleString()}</strong> sessions</span>`,
    `<span>${data.sessions_with_timestamps.toLocaleString()} dated</span>`,
    `<span>${data.orphan_sessions.toLocaleString()} orphans (${formatBytes(data.orphan_bytes)})</span>`,
    `<span>${data.sessions_with_headers.toLocaleString()} header-linked</span>`,
  ].join('<span class="summary-strip-sep">·</span>');
  root.appendChild(strip);

  const tabs = createTabBar([
    { id: "month", label: "By month" },
    { id: "repo", label: "By repo" },
    { id: "sessions", label: "Sessions" },
  ]);
  root.appendChild(tabs.bar);
  root.appendChild(tabs.panels);

  const monthPanel = tabs.getPanel("month");
  monthPanel.innerHTML = `
    <div class="panel-header">
      <div>
        <strong>Storage by month</strong>
        <div class="muted">Current storage totals grouped by session created or updated month (single snapshot).</div>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th>Month</th>
          <th>Sessions</th>
          <th>Header-linked</th>
          <th>Orphan</th>
          <th>Total</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  `;
  const monthBody = monthPanel.querySelector("tbody")!;
  for (const row of data.by_month) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(row.month)}</td>
      <td class="size">${row.session_count.toLocaleString()}</td>
      <td class="size">${formatBytes(row.header_linked_bytes)}</td>
      <td class="size">${formatBytes(row.orphan_bytes)}</td>
      <td class="size">${formatBytes(row.bytes)}</td>
    `;
    monthBody.appendChild(tr);
  }

  const repoPanel = tabs.getPanel("repo");
  repoPanel.innerHTML = `
    <div class="panel-header"><strong>Repositories by activity span</strong></div>
    <table>
      <thead>
        <tr>
          <th>Repository</th>
          <th>Sessions</th>
          <th>First</th>
          <th>Last</th>
          <th>Size</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  `;
  const repoBody = repoPanel.querySelector("tbody")!;
  for (const row of data.repos.slice(0, 25)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(row.repo)}</td>
      <td class="size">${row.session_count.toLocaleString()}</td>
      <td>${escapeHtml(row.first_seen || "—")}</td>
      <td>${escapeHtml(row.last_seen || "—")}</td>
      <td class="size">${formatBytes(row.bytes)}</td>
    `;
    tr.onclick = () => navigate({ view: "sessions", repo: row.repo });
    repoBody.appendChild(tr);
  }

  const sessionPanel = tabs.getPanel("sessions");
  sessionPanel.innerHTML = `
    <div class="panel-header">
      <div>
        <strong>Sessions by date</strong>
        <div class="muted">Showing ${Math.min(15, data.sessions.length)} of ${data.sessions.length}</div>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Name</th>
          <th>Header</th>
          <th>Size</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  `;
  const sessionBody = sessionPanel.querySelector("tbody")!;
  for (const row of data.sessions.slice(0, 15)) {
    const tr = document.createElement("tr");
    const date = row.created_date || row.last_updated_date || "(unknown)";
    const label = row.name || row.subtitle || row.composer_id;
    const repo = row.repo || "(unknown)";
    tr.innerHTML = `
      <td>${escapeHtml(date)}</td>
      <td>
        <div>${escapeHtml(label)}</div>
        <div class="muted">${escapeHtml(repo)}</div>
      </td>
      <td>${row.has_header ? "yes" : "no"}</td>
      <td class="size">${formatBytes(row.bytes)}</td>
    `;
    tr.onclick = () => navigate({ view: "session", repo, composerId: row.composer_id });
    sessionBody.appendChild(tr);
  }

  tabs.show("month");

  if (data.notes.length) {
    const notesSection = createCollapsibleSection("Notes", {
      defaultOpen: false,
      secondary: true,
      subtitle: `${data.notes.length} caveat${data.notes.length === 1 ? "" : "s"}`,
    });
    const list = document.createElement("ul");
    list.className = "agentkv-notes muted";
    for (const note of data.notes) {
      const li = document.createElement("li");
      li.textContent = note;
      list.appendChild(li);
    }
    notesSection.body.appendChild(list);
    root.appendChild(notesSection.section);
  }

  viewEl.innerHTML = "";
  viewEl.appendChild(root);
}

async function renderTimeline() {
  if (timelineCache) {
    paintTimeline(timelineCache);
    return;
  }
  setLoading("Building timeline…");
  try {
    timelineCache = await getTimeline();
    paintTimeline(timelineCache);
  } catch (error) {
    setError(error);
  }
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function render() {
  try {
    if (route.view === "repos") {
      await renderRepos();
    } else if (route.view === "timeline") {
      await renderTimeline();
    } else if (route.view === "sessions") {
      await renderSessions(route.repo);
    } else {
      await renderSession(route.repo, route.composerId);
    }
  } catch (error) {
    setError(error);
  }
}

async function boot() {
  renderBreadcrumbs();
  setLoading("Opening database…");
  try {
    const status = await openDatabase();
    setStatus(
      status.live
        ? `Live read-only: ${status.database}`
        : `Snapshot: ${status.database}`,
    );
    await render();
  } catch (error) {
    setError(
      `${error instanceof Error ? error.message : error}\n\nStart the API with: python -m cursorscope serve`,
    );
  }
}

void boot();
