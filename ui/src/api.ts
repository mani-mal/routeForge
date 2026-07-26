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

export interface Controls {
  headroom_enabled: boolean;
  min_tokens: number;
  deferred_controls: string[];
}

export interface TrafficStartResult {
  status: string;
  requests: number;
}

export interface TrafficStatus {
  running: boolean;
  last_summary: Record<string, unknown> | null;
}

export type StreamEvent =
  | { type: "request"; summary: RequestSummary }
  | { type: "step"; request_id: string; record: TraceRecord; index: number };

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export const api = {
  requests: (limit = 100) => fetch(`/api/requests?limit=${limit}`).then(j<RequestSummary[]>),
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
  // Fires the live traffic burst (Phase 7 Task 4): the server self-calls
  // its own /v1/chat/completions in-process, so the dashboard fills live.
  startTraffic: (requests = 100, rps = 20) =>
    fetch(`/api/traffic`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requests, rps }),
    }).then(j<TrafficStartResult>),
  trafficStatus: () => fetch(`/api/traffic`).then(j<TrafficStatus>),
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

// Scripted requests for the control panel (canned bodies; keys map to personas).
export const SCRIPTED: { label: string; apiKey: string; body: object; hint: string }[] = [
  {
    label: "Clean question",
    apiKey: "acme-key",
    body: { messages: [{ role: "user", content: "What is the capital of France?" }] },
    hint: "public → cheap local tier",
  },
  {
    label: "PII (SSN)",
    apiKey: "hospital-key",
    body: {
      messages: [{ role: "user", content: "My SSN is 123-45-6789 — summarize my account status." }],
    },
    hint: "restricted → onprem_only, cloud greyed out",
  },
  {
    label: "Multi-step task",
    apiKey: "acme-key",
    body: {
      messages: [{ role: "user", content: "Look up account 42 then compute 12 * 7." }],
      mock_multistep: true,
    },
    hint: "agent hops re-enter the router",
  },
  {
    label: "High-risk (PHI)",
    apiKey: "hospital-key",
    body: {
      messages: [
        { role: "user", content: "Patient medical license ML-4471 — advise on treatment plan." },
      ],
    },
    hint: "high-risk onprem → require_verification",
  },
];
