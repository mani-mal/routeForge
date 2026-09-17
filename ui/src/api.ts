// API client + shared types — the single contract the UI consumes.
// Matches the Phase-6a backend (app/api/ui_routes.py, app/api/routes.py).
// The Vite dev server proxies /api and /v1 to the backend (vite.config.ts).

import { TIER_HEX } from "./tokens";

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
  ts?: string | null;
  total_tokens: number;
  quality_sum: number; // Σ per-call quality — Token/Cost Intelligence Density numerator
  // Chosen model's quality score + its provenance (tier_default | calibrated |
  // published | customer — see app/routing/quality.py). A tier-quality PROXY
  // in the demo, NOT measured accuracy; always show provenance alongside it.
  quality?: number | null;
  quality_source?: string | null;
  session_id?: string | null; // B17: client workflow-run correlation id (opaque); groups rows
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
  display?: { mode: "off" | "redacted" | "full"; input: string | null; output: string | null };
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
  // Quality-per-resource, same formula as app/api/history.py's stats_over —
  // a tier-quality PROXY, not measured accuracy.
  token_intelligence_density: number;
  cost_intelligence_density: number;
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
  onprem_violations: number; // onprem_only requests served off-prem — a real Rule-3 breach
  latency_p50_ms: number;
  tier_distribution: Record<string, number>;
  token_intelligence_density: number;
  cost_intelligence_density: number;
}

export interface Controls {
  headroom_enabled: boolean;
  min_tokens: number;
  deferred_controls: string[];
}

export type StreamEvent =
  | { type: "request"; summary: RequestSummary }
  | { type: "step"; request_id: string; record: TraceRecord; index: number };

// Pull the human-facing message out of a non-2xx response. FastAPI answers
// `{"detail": "..."}` for an HTTPException and `{"detail": [{loc, msg}, …]}`
// for a request-validation failure. The admin policy clamps are the authority
// on every posture decision and their 422 `detail` names which rule fired and
// what to do instead — that text IS the product's guidance, so it must reach
// the operator instead of a bare "422 Unprocessable Entity". Falls back to the
// status line when the body isn't JSON (proxy/HTML error pages).
async function errDetail(r: Response): Promise<string> {
  const status = `${r.status} ${r.statusText}`;
  try {
    const body = (await r.json()) as { detail?: unknown };
    const d = body?.detail;
    if (typeof d === "string" && d.trim()) return d;
    if (Array.isArray(d) && d.length) {
      const parts = (d as { loc?: (string | number)[]; msg?: string }[])
        .map((e) => [(e.loc ?? []).slice(1).join("."), e.msg].filter(Boolean).join(": "))
        .filter(Boolean);
      if (parts.length) return parts.join("; ");
    }
  } catch {
    /* non-JSON error body; keep the status line */
  }
  return status;
}

// Every caller that renders `e.message` (PolicyEditor, InfrastructurePanel,
// PrivacyRulesEditor) gets the server's explanation for free; callers that
// only care that it threw are unaffected.
async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(await errDetail(r));
  return (await r.json()) as T;
}

// The /api/* console surface is admin-gated (app/apps/admin_auth.require_admin).
// Every read/control call must carry the admin session cookie. `same-origin` is
// the Fetch default, but state it explicitly so intent survives refactors and a
// reverse-proxy deploy (UI nginx proxies /api same-origin to the API pod).
const CRED: RequestCredentials = "same-origin";

export const api = {
  requests: (limit = 100, app?: string) =>
    fetch(`/api/requests?limit=${limit}${app ? `&app=${encodeURIComponent(app)}` : ""}`,
      { credentials: CRED })
      .then(j<RequestSummary[]>),
  fleet: () => fetch(`/api/fleet`, { credentials: CRED }).then(j<FleetRow[]>),
  request: (id: string) => fetch(`/api/requests/${id}`, { credentials: CRED }).then(j<ChatResponse>),
  stats: () => fetch(`/api/stats`, { credentials: CRED }).then(j<Stats>),
  controls: () => fetch(`/api/controls`, { credentials: CRED }).then(j<Controls>),
  setControls: (headroom_enabled: boolean) =>
    fetch(`/api/controls`, {
      method: "POST",
      credentials: CRED,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ headroom_enabled }),
    }).then(j<Controls>),
  feedback: (requestId: string, body: { score_name: string; value: number | string;
             comment?: string; corrected_output?: string }) =>
    fetch(`/api/requests/${requestId}/feedback`, {
      method: "POST",
      credentials: CRED,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(j<{ stored: boolean; reason?: string }>),
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
  max_cost_usd_per_request: number | null;
  max_rpm: number | null;
  compression_enabled: boolean | null;   // null = inherit global Headroom toggle
  capture_io: string | null;   // null = inherit global capture_io; off|redacted|full
  // --- per-app protection posture + detector overrides (PolicyIn, app/api/admin_routes.py).
  // Every one of these is clamped server-side; the UI explains the clamps, it
  // never invents its own. See docs/reference/per-app-config.md.
  pii_detection: string | null;   // null = inherit the deployment's live global detector; PII_MODES
  stream_mode: string | null;   // null = inherit (inbound stream rejected); STREAM_MODES
  policy_mode: string | null;   // null = inherit serving.policy.default_policy_mode; POLICY_MODES
  pii_entity_denylist: string[] | null;   // null = inherit (nothing exempt); detect-but-don't-redact types
  pii_score_threshold: number | null;   // null = inherit (no floor); 0.0–1.0 min Presidio confidence
  pii_exempt_patterns: string[] | null;   // null = inherit (none); regexes fullmatched against the entity VALUE
}

// The PUT body: a policy plus the one escape from its merge semantics. `null`
// in the body still means "leave the stored value alone"; `reset` names the
// fields to write back to NULL ("inherit the deployment default"). Naming a
// field in `reset` while also sending a value for it is a 422, so the editor
// only ever lists fields it is sending as null.
export interface AdminPolicyWrite extends AdminPolicy {
  reset?: string[];
}

export interface AdminApp {
  id: string;
  name: string;
  owner_email: string | null;
  status: string;
  default_residency: string;
  langfuse_project_id: string | null;
  langfuse_host?: string | null;
  has_langfuse_keys?: boolean;
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
  // Quality-per-resource + its provenance breakdown (app/apps/store.py
  // usage_summary), mirroring history.py's stats_over formula exactly. A
  // tier-quality PROXY (or calibrated/customer when set) — not measured
  // accuracy; quality_source_distribution shows how much of it is which.
  token_intelligence_density: number;
  cost_intelligence_density: number;
  quality_source_distribution: Record<string, number>;
}

// Per-application rollup for the cross-app usage report (durable, DB-backed).
// Backend: GET /admin/usage/by-application?hours= (app/api/admin_routes.py).
export interface UsageByApp {
  application_id: string;
  count: number;
  rejected_count: number;
  total_usd: number;
  baseline_usd: number;
  total_tokens: number;
  onprem_count: number;
  tier_distribution: Record<string, number>;
  model_distribution: Record<string, number>;
  sensitivity_distribution: Record<string, number>;
  token_intelligence_density: number;
  cost_intelligence_density: number;
  quality_source_distribution: Record<string, number>;
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

// The deployment-wide policy envelope every application sits inside, returned
// read-only by GET /admin/me (app/api/admin_routes.py). The console needs it to
// be honest about two things it otherwise has to guess: (1) what a per-app
// field left null actually inherits — "(global default)" on its own tells an
// operator nothing; (2) which postures the deployment ceilings forbid outright.
// It is never editable here: an admin-editable ceiling would not be a ceiling.
export interface DeploymentEnvelope {
  default_policy_mode: string;
  default_pii_detection: string;
  default_capture_io: string;
  reject_stream_requests: boolean;
  allow_app_passthrough: boolean;
  allow_app_pii_downgrade: boolean;
}

// `deployment` is optional so the console still boots (and still authenticates)
// against a server that predates the envelope; consumers fall back to naming
// nothing rather than guessing a value.
export interface AdminMe {
  admin_id: string;
  deployment?: DeploymentEnvelope;
}

export const adminApi = {
  me: () => fetch("/admin/me", { credentials: "same-origin" }).then(j<AdminMe>),
  login: (email: string, password: string) =>
    fetch("/admin/login", opts("POST", { email, password })).then(j<{ email: string }>),
  logout: () => fetch("/admin/logout", opts("POST")).then(j<{ ok: boolean }>),
  apps: () => fetch("/admin/applications", { credentials: "same-origin" }).then(j<AdminApp[]>),
  createApp: (body: { id: string; name: string; owner_email?: string; default_residency?: string }) =>
    fetch("/admin/applications", opts("POST", body)).then(j<{ application: AdminApp; token: string }>),
  updateApp: (id: string, body: Partial<{ name: string; owner_email: string; status: string; default_residency: string }>) =>
    fetch(`/admin/applications/${id}`, opts("PATCH", body)).then(j<AdminApp>),
  setPolicy: (id: string, body: AdminPolicyWrite) =>
    fetch(`/admin/applications/${id}/policy`, opts("PUT", body)).then(j<AdminApp>),
  // Manual per-app Langfuse creds (B15) — for an air-gapped operator who can't
  // reach the org-scoped auto-provisioning API. Secret is write-only: never
  // echoed back in AdminApp; only `has_langfuse_keys` reflects it was set.
  setLangfuseCreds: (id: string, body: { host: string; public_key: string; secret_key: string; project_id: string }) =>
    fetch(`/admin/applications/${id}/langfuse`, opts("PUT", body)).then(j<AdminApp>),
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
  usageByApplication: (hours?: number) => {
    const q = new URLSearchParams();
    if (hours) q.set("hours", String(hours));
    return fetch(`/admin/usage/by-application?${q.toString()}`, { credentials: "same-origin" }).then(
      j<{ apps: UsageByApp[]; window_hours: number | null }>,
    );
  },
};

// --- Model onboarding (pure router; Task 5/7) -------------------------------
// routeForge deploys nothing — operators stand up an OpenAI-compatible
// endpoint themselves and onboard it here at runtime (name, slug, tier,
// endpoint, price, which apps may use it). Backend: app/api/admin_routes.py —
// POST/GET /admin/infra/models, PUT/DELETE /admin/infra/models/{slug}.

export type OnboardedModel = {
  slug: string; display_name: string; tier: string; provider: string;
  endpoint: string; api_key_env: string | null;
  price_input: number; price_output: number;
  quality: number | null; context_window: number | null;
  enabled: boolean; allowed_app_ids: string[] | null;
  probe_status: string; probe_error: string | null; notes: string | null;
};
export type OnboardIn = {
  slug: string; display_name: string; tier: string; endpoint: string;
  api_key_env?: string | null; price_input: number; price_output: number;
  quality?: number | null; context_window?: number | null;
  allowed_app_ids?: string[] | null; notes?: string | null;
  enabled?: boolean;
};
export const onboardApi = {
  list: () => fetch("/admin/infra/models", { credentials: CRED }).then(j<OnboardedModel[]>),
  create: (body: OnboardIn) =>
    fetch("/admin/infra/models", opts("POST", body)).then(j<OnboardedModel & {enabled: boolean}>),
  update: (slug: string, body: Partial<OnboardIn>) =>
    fetch(`/admin/infra/models/${encodeURIComponent(slug)}`, opts("PUT", body)).then(j<OnboardedModel>),
  remove: (slug: string) =>
    fetch(`/admin/infra/models/${encodeURIComponent(slug)}`, opts("DELETE")).then(j<{ ok: boolean }>),
};

// --- Per-app privacy rules (custom recognizers + sensitivity rules) --------
// Backend: app/api/admin_routes.py — GET/PUT /admin/applications/{id}/privacy.

export interface Recognizer {
  name: string;
  regex: string;
  score: number;
}

export interface SensitivityRule {
  entities: string[];
  sensitivity: string;
  risk_class: string;
  residency: string;
  require_verification: boolean;
}

export interface PrivacyProfile {
  recognizers: Recognizer[];
  rules: SensitivityRule[];
}

export async function getPrivacy(appId: string): Promise<PrivacyProfile> {
  return fetch(`/admin/applications/${appId}/privacy`, { credentials: "same-origin" }).then(j<PrivacyProfile>);
}

// The backend's PUT handler only ever returns `{"ok": true}` on success — it
// does not echo the stored profile. Callers must re-fetch via getPrivacy() to
// see what was actually persisted (and, on Reset, the global-fallback rules
// the server now serves).
export async function putPrivacy(appId: string, profile: PrivacyProfile): Promise<void> {
  const r = await fetch(`/admin/applications/${appId}/privacy`, opts("PUT", profile));
  if (!r.ok) throw new Error(await errDetail(r));
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
  { key: "task_mode", label: "Task Mode", caption: "Direct — agent loops run in the client" },
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

// Cheap/mid/frontier come from the single token source (tokens.ts). "?" is a
// local fallback for an unresolved tier — not part of the semantic palette,
// so it isn't in tokens.ts — kept here so existing `TIER_COLOR[t] ?? TIER_COLOR["?"]`
// call sites keep their grey fallback behaviour unchanged.
export const TIER_COLOR: Record<string, string> = {
  ...TIER_HEX,
  "?": "#6b7280",
};
