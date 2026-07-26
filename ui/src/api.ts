// API client + shared types — the single contract the UI consumes.
// Matches the Phase-6a backend (app/api/ui_routes.py, app/api/routes.py).
// The Vite dev server proxies /api and /v1 to the backend (vite.config.ts).

export type Tier = "cheap" | "mid" | "frontier" | "?";

export interface RequestSummary {
  id: string;
  tenant_id: string;
  sensitivity: string;
  onprem_only: boolean;
  model: string;
  model_display: string; // human-facing model name; UI renders this, `model` is the slug
  tier: Tier;
  total_usd: number;
  baseline_usd: number;
  routing_saved_usd: number;
  compression_saved_usd: number;
  latency_ms: number;
  hops: number;
  detected_entities: string[]; // TYPES only, never raw values
  created_seq: number;
}

export interface RejectedCandidate {
  name: string;
  provider: string;
  tier: string;
  reason: string;
  display_name?: string | null; // human-facing name; falls back to `name`
}

// A trace record is one of several step shapes; fields are optional per step.
export interface TraceRecord {
  step: string;
  chosen?: string | null;
  chosen_display_name?: string | null; // human-facing name for `chosen`
  reason?: string;
  rejected?: RejectedCandidate[];
  est_cost_usd?: number;
  actual_cost_usd?: number | null;
  latency_ms?: number;
  hop_index?: number;
  tokens_priced?: number;
  // verify
  required?: boolean;
  performed?: boolean;
  passed?: boolean;
  entities_found?: string[];
  escalated?: boolean;
  // compression
  enabled?: boolean;
  applied?: boolean;
  bypass_reason?: string | null;
  tokens_before?: number;
  tokens_after?: number;
  reduction_pct?: number;
  compressor?: string | null;
  ccr_stored?: boolean;
  ccr_retrievals?: number;
  ccr_retrievals_to_cloud?: number;
}

export interface ChatResponse {
  id: string;
  model: string;
  model_display?: string; // human-facing model name (falls back to `model`)
  langfuse_url?: string | null; // live deep-link to this request's Langfuse trace
  choices: { index: number; message: { role: string; content: string }; finish_reason: string }[];
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  trace: TraceRecord[];
  cost: {
    total_usd: number;
    baseline_usd: number;
    routing_saved_usd: number;
    compression_saved_usd: number;
  };
  policy: {
    sensitivity: string;
    onprem_only: boolean;
    allowed_providers: string[];
    detected_entities: string[];
  };
}

export interface Stats {
  count: number;
  total_usd: number;
  baseline_usd: number;
  routing_saved_usd: number;
  compression_saved_usd: number;
  onprem_count: number;
  tier_distribution: Record<string, number>;
  latency_p50_ms: number;
  latency_p95_ms: number;
}

export interface FleetRow {
  app: string;
  count: number;
  total_usd: number;
  baseline_usd: number;
  routing_saved_usd: number;
  compression_saved_usd: number;
  onprem_count: number;
  pii_count: number;
  latency_p50_ms: number;
  tier_distribution: Record<string, number>;
}

export interface Controls {
  headroom_enabled: boolean;
  min_tokens: number;
  deferred_controls: string[];
}

export type StreamEvent =
  | { type: "request"; summary: RequestSummary }
  | { type: "step"; request_id: string; record: TraceRecord; index: number };

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export const api = {
  requests: (limit = 100, app?: string) =>
    fetch(`/api/requests?limit=${limit}${app ? `&app=${encodeURIComponent(app)}` : ""}`)
      .then(j<RequestSummary[]>),
  fleet: () => fetch(`/api/fleet`).then(j<FleetRow[]>),
  request: (id: string) => fetch(`/api/requests/${id}`).then(j<ChatResponse>),
  stats: () => fetch(`/api/stats`).then(j<Stats>),
  controls: () => fetch(`/api/controls`).then(j<Controls>),
  setControls: (headroom_enabled: boolean) =>
    fetch(`/api/controls`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ headroom_enabled }),
    }).then(j<Controls>),
  chat: (body: object, apiKey: string) =>
    fetch(`/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(body),
    }).then(j<ChatResponse>),
};

// Subscribe to the live decision stream. Returns an unsubscribe fn.
// `onStatus` (optional) reports the live connection state so the UI's "live"
// dot stays truthful — EventSource silently auto-reconnects on drop.
export function subscribeStream(
  onEvent: (e: StreamEvent) => void,
  onStatus?: (connected: boolean) => void,
): () => void {
  const es = new EventSource(`/api/stream`);
  es.onopen = () => onStatus?.(true);
  es.onerror = () => onStatus?.(false); // dropped; EventSource retries in bg
  es.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data) as StreamEvent);
    } catch {
      /* ignore heartbeats / malformed lines */
    }
  };
  return () => es.close();
}

// --- Admin API (multi-tenant console; spec 2026-07-25) ---------------------

export interface AdminToken {
  id: string;
  prefix: string;
  name: string;
  status: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface AdminPolicy {
  allowed_tiers: string[] | null;
  allowed_providers: string[] | null;
  allowed_models: string[] | null;
  residency: string | null;
  max_spend_usd_per_day: number | null;
  max_rpm: number | null;
}

export interface AdminApp {
  id: string;
  name: string;
  owner_email: string | null;
  status: string;
  default_residency: string;
  langfuse_project_id: string | null;
  created_at: string | null;
  policy: AdminPolicy | null;
  tokens: AdminToken[];
}

export interface UsageSummary {
  count: number;
  rejected_count: number;
  total_usd: number;
  baseline_usd: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  onprem_count: number;
  tier_distribution: Record<string, number>;
  model_distribution: Record<string, number>;
  application_distribution: Record<string, number>;
  latency_p50_ms: number;
  latency_p95_ms: number;
}

// Admin routes use a session cookie; same-origin (Vite proxies /admin) so the
// browser sends it automatically. `credentials: "same-origin"` is the default,
// set explicitly for clarity.
const opts = (method: string, body?: object): RequestInit => ({
  method,
  credentials: "same-origin",
  headers: { "Content-Type": "application/json" },
  ...(body ? { body: JSON.stringify(body) } : {}),
});

export const adminApi = {
  me: () => fetch("/admin/me", { credentials: "same-origin" }).then(j<{ admin_id: string }>),
  login: (email: string, password: string) =>
    fetch("/admin/login", opts("POST", { email, password })).then(j<{ email: string }>),
  logout: () => fetch("/admin/logout", opts("POST")).then(j<{ ok: boolean }>),
  apps: () => fetch("/admin/applications", { credentials: "same-origin" }).then(j<AdminApp[]>),
  createApp: (body: { id: string; name: string; owner_email?: string; default_residency?: string }) =>
    fetch("/admin/applications", opts("POST", body)).then(j<{ application: AdminApp; token: string }>),
  updateApp: (id: string, body: Partial<{ name: string; owner_email: string; status: string; default_residency: string }>) =>
    fetch(`/admin/applications/${id}`, opts("PATCH", body)).then(j<AdminApp>),
  setPolicy: (id: string, body: AdminPolicy) =>
    fetch(`/admin/applications/${id}/policy`, opts("PUT", body)).then(j<AdminApp>),
  createToken: (id: string, name: string) =>
    fetch(`/admin/applications/${id}/tokens`, opts("POST", { name })).then(j<{ token: string; id: string; prefix: string }>),
  revokeToken: (tokenId: string) =>
    fetch(`/admin/tokens/${tokenId}/revoke`, opts("POST")).then(j<{ ok: boolean }>),
  usage: (applicationId?: string, hours?: number) => {
    const q = new URLSearchParams();
    if (applicationId) q.set("application_id", applicationId);
    if (hours) q.set("hours", String(hours));
    return fetch(`/admin/usage?${q.toString()}`, { credentials: "same-origin" }).then(j<UsageSummary>);
  },
};

// The ten serving-plane steps, in order, for the flow visualizer. `caption` is
// the one-line "what this step does", shown on the node and in the step inspector.
export interface StepMeta {
  key: string;
  label: string;
  caption: string;
}
export const STEPS: StepMeta[] = [
  { key: "request", label: "Request", caption: "Incoming OpenAI-compatible call" },
  { key: "gate", label: "Policy Gate", caption: "Detect & redact PII/PHI, set residency" },
  { key: "context", label: "Policy Context", caption: "Immutable policy travels with the request" },
  { key: "task_mode", label: "Task Mode", caption: "Direct answer vs. multi-step agent" },
  { key: "compression", label: "Compression", caption: "Headroom shrinks tool output & history" },
  { key: "router", label: "Model Router", caption: "Picks model + tier per call, by cost & risk" },
  { key: "gateway", label: "Gateway", caption: "Quotas, breakers, fallback on failure" },
  { key: "backend", label: "Backend", caption: "Runs on the chosen serving path" },
  { key: "verify", label: "Verify", caption: "Leakage check on the completion" },
  { key: "response", label: "Response", caption: "Governed answer + full decision trace" },
];

export function stepMeta(key: string): StepMeta | undefined {
  return STEPS.find((s) => s.key === key);
}

export const TIER_COLOR: Record<string, string> = {
  cheap: "#22c55e",
  mid: "#3b82f6",
  frontier: "#ef4444",
  "?": "#6b7280",
};
