const API_BASE = import.meta.env.VITE_API_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  const data = await response.json();
  if (!response.ok) {
    throw new Error((data as { error?: string }).error ?? `HTTP ${response.status}`);
  }
  return data as T;
}

export type Status = {
  open: boolean;
  source?: string;
  database?: string;
  live?: boolean;
  fast?: boolean;
};

export type RepoRow = {
  repo: string;
  bytes: number;
  session_count: number;
  confidence: string;
};

export type OtherStorageRow = {
  label: string;
  bytes: number;
  count: number;
  description: string;
};

export type GrowthDelta = {
  label: string;
  previous: number;
  current: number;
  delta: number;
};

export type GrowthResponse = {
  headline: string | null;
  backup_headline: string | null;
  comparison_title?: string | null;
  delta_file_bytes: number;
  delta_kv_bytes: number;
  delta_by_type: GrowthDelta[];
  delta_top_repos?: GrowthDelta[];
  previous: Record<string, unknown> | null;
  backup: Record<string, unknown> | null;
  current: Record<string, unknown>;
  saved: boolean;
  notes: string[];
};

export type CheckpointRecord = {
  id: string;
  label: string | null;
  note: string | null;
  recorded_at: string;
  database_file_bytes: number;
  cursor_disk_kv_bytes: number;
  kind: string;
};

export type ReposResponse = {
  repos: RepoRow[];
  database_file_bytes: number;
  cursor_disk_kv_bytes: number;
  repos_attributed_bytes: number;
  unknown_session_bytes: number;
  table_total_bytes: number;
  other_storage_bytes: number;
  other_storage_breakdown: OtherStorageRow[];
  unattributed: Record<string, number>;
  integrity: Record<string, number | string>;
  growth?: GrowthResponse | null;
};

export type SessionRow = {
  composer_id: string;
  bytes: number;
  name?: string | null;
  subtitle?: string | null;
  repo?: string | null;
  repo_confidence: string;
  has_header: boolean;
  breakdown: Record<string, number>;
};

export type SessionsResponse = {
  total_matching: number;
  sessions: SessionRow[];
};

export type SessionDetail = {
  composer_id: string;
  found: boolean;
  name?: string | null;
  subtitle?: string | null;
  repo?: string | null;
  repo_confidence: string;
  total_bytes: number;
  storage_breakdown: Record<string, number>;
  files: { path: string; source: string }[];
  bubbles: { bubble_id: string; bytes: number }[];
  checkpoints: { checkpoint_id: string; bytes: number }[];
  notes: string[];
};

export async function openDatabase(opts?: {
  ephemeral?: boolean;
  cached_snapshot?: boolean;
}): Promise<Status> {
  const body: Record<string, unknown> = {
    fast: true,
    scan_agentkv: false,
  };
  if (opts?.ephemeral) {
    body.ephemeral = true;
  } else if (opts?.cached_snapshot) {
    body.cached_snapshot = true;
  }
  return request<Status>("/api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getStatus(): Promise<Status> {
  return request<Status>("/api/status");
}

export function formatBytes(bytes: number | null | undefined): string {
  const n = Number(bytes);
  if (!Number.isFinite(n)) {
    return "—";
  }
  let value = n;
  const units = ["B", "KB", "MB", "GB", "TB"];
  for (const unit of units) {
    if (Math.abs(value) < 1024 || unit === "TB") {
      return `${value.toFixed(value >= 10 || unit === "B" ? 0 : 2)} ${unit}`;
    }
    value /= 1024;
  }
  return `${n} B`;
}

export function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

async function overviewFallback(repos: RepoRow[]): Promise<ReposResponse> {
  const ov = await request<Record<string, unknown>>("/api/overview");
  const pages = (ov.pages ?? {}) as Record<string, unknown>;
  const unattributed = (ov.unattributed ?? {}) as Record<string, number>;
  const reposAttributed = num(ov.repos_attributed_bytes) || repos.reduce((sum, row) => sum + num(row.bytes), 0);
  const unknownSession = num(ov.unknown_session_bytes);
  const kvBytes = num(ov.cursor_disk_kv_bytes);
  const fileBytes = num(pages.file_bytes);
  return {
    repos,
    database_file_bytes: fileBytes,
    cursor_disk_kv_bytes: kvBytes,
    repos_attributed_bytes: reposAttributed,
    unknown_session_bytes: unknownSession,
    table_total_bytes: reposAttributed + unknownSession,
    other_storage_bytes: Math.max(0, kvBytes - reposAttributed - unknownSession),
    other_storage_breakdown: (ov.other_storage_breakdown ?? []) as OtherStorageRow[],
    unattributed,
    integrity: (ov.integrity ?? {}) as Record<string, number | string>,
    growth: (ov.growth ?? null) as GrowthResponse | null,
  };
}

function normalizeReposResponse(raw: unknown): ReposResponse | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const payload = raw as Record<string, unknown>;
  const nested = payload.repos;
  if (nested && typeof nested === "object" && !Array.isArray(nested) && Array.isArray((nested as ReposResponse).repos)) {
    return nested as ReposResponse;
  }
  if (!Array.isArray(nested)) {
    return null;
  }
  const repos = nested as RepoRow[];
  if (payload.database_file_bytes != null) {
    return {
      repos,
      database_file_bytes: num(payload.database_file_bytes),
      cursor_disk_kv_bytes: num(payload.cursor_disk_kv_bytes),
      repos_attributed_bytes: num(payload.repos_attributed_bytes),
      unknown_session_bytes: num(payload.unknown_session_bytes),
      table_total_bytes: num(payload.table_total_bytes) || repos.reduce((sum, row) => sum + num(row.bytes), 0) + num(payload.unknown_session_bytes),
      other_storage_bytes: num(payload.other_storage_bytes),
      other_storage_breakdown: (payload.other_storage_breakdown ?? []) as OtherStorageRow[],
      unattributed: (payload.unattributed ?? {}) as Record<string, number>,
      integrity: (payload.integrity ?? {}) as Record<string, number | string>,
      growth: (payload.growth ?? null) as GrowthResponse | null,
    };
  }
  return null;
}

export async function listRepos(): Promise<ReposResponse> {
  const raw = await request<unknown>("/api/repos");
  const normalized = normalizeReposResponse(raw);
  if (normalized) {
    return normalized;
  }
  const repos = Array.isArray(raw)
    ? (raw as RepoRow[])
    : Array.isArray((raw as { repos?: RepoRow[] }).repos)
      ? (raw as { repos: RepoRow[] }).repos
      : [];
  return overviewFallback(repos);
}

export type ScanProgress = {
  active: boolean;
  label?: string;
  current?: number;
  total?: number;
  percent?: number;
  cached?: boolean;
  finishing?: boolean;
  eta_sec?: number | null;
};

export async function getScanProgress(): Promise<ScanProgress> {
  return request<ScanProgress>("/api/scan/progress");
}

export async function listSessions(repo?: string): Promise<SessionsResponse> {
  const params = new URLSearchParams({ limit: "500" });
  if (repo) {
    params.set("repo", repo);
  }
  return request<SessionsResponse>(`/api/sessions?${params}`);
}

export async function getSession(composerId: string): Promise<SessionDetail> {
  return request<SessionDetail>(`/api/sessions/${encodeURIComponent(composerId)}`);
}

export type AgentKvAnalysis = {
  total_blobs: number;
  total_bytes: number;
  referenced_blobs: number;
  referenced_bytes_unique: number;
  unreferenced_blobs: number;
  unreferenced_bytes: number;
  structural_reference_rows: number;
  composer_content_blobs: number;
  composer_content_bytes: number;
  overlap_blobs: number;
  overlap_bytes: number;
  referenced_ratio: number;
  unreferenced_ratio: number;
  elapsed_sec: number;
  notes: string[];
  top_attributed_sessions: { composer_id: string; bytes: number }[];
};

export async function analyzeAgentKv(useCache = true): Promise<AgentKvAnalysis> {
  return request<AgentKvAnalysis>("/api/agentkv/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ use_cache: useCache }),
  });
}

export type TimelineMonth = {
  month: string;
  session_count: number;
  bytes: number;
  header_linked_bytes: number;
  orphan_bytes: number;
  unknown_date_bytes: number;
};

export type TimelineRepo = {
  repo: string;
  session_count: number;
  bytes: number;
  first_seen: string | null;
  last_seen: string | null;
  confidence: string;
};

export type TimelineSession = {
  composer_id: string;
  bytes: number;
  repo: string | null;
  repo_confidence: string;
  name: string | null;
  subtitle: string | null;
  created_at: number | null;
  last_updated_at: number | null;
  created_date: string | null;
  last_updated_date: string | null;
  has_header: boolean;
  is_archived: boolean | null;
};

export type TimelineResponse = {
  sessions_total: number;
  sessions_with_timestamps: number;
  sessions_with_headers: number;
  orphan_sessions: number;
  orphan_bytes: number;
  unknown_date_sessions: number;
  unknown_date_bytes: number;
  by_month: TimelineMonth[];
  repos: TimelineRepo[];
  sessions: TimelineSession[];
  notes: string[];
};

export async function getTimeline(repo?: string): Promise<TimelineResponse> {
  const params = new URLSearchParams({ limit: "500" });
  if (repo) {
    params.set("repo", repo);
  }
  return request<TimelineResponse>(`/api/timeline?${params}`);
}

export async function listCheckpoints(): Promise<{ checkpoints: CheckpointRecord[] }> {
  return request<{ checkpoints: CheckpointRecord[] }>("/api/checkpoints?limit=50");
}

export async function createCheckpoint(
  label: string,
  note?: string,
): Promise<CheckpointRecord> {
  return request<CheckpointRecord>("/api/checkpoints", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, note: note || undefined }),
  });
}

export async function diffCheckpoints(
  fromRef: string,
  toRef: string,
): Promise<GrowthResponse> {
  const params = new URLSearchParams({ from: fromRef, to: toRef });
  return request<GrowthResponse>(`/api/checkpoints/diff?${params}`);
}

export async function deleteCheckpoint(ref: string): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(`/api/checkpoints/${encodeURIComponent(ref)}`, {
    method: "DELETE",
  });
}
