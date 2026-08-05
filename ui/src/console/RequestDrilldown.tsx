// Request drill-down — two altitudes over one request. Replaces the old
// three-panel trio (FlowVisualizer + DecisionInspector + CompressionView),
// which get deleted once this is wired in. Top band is a plain-language,
// owner-facing summary + the greyed-provider privacy strip; bottom band is
// the full admin-facing technical trace (chosen vs rejected, per-hop +
// cumulative cost, compression/CCR counts, verification, Langfuse link).
//
// Honesty rules carried over from the panels this replaces: entity fields
// are TYPES only (never raw values/text), CCR fields are COUNTS only (never
// cached content), and excluded providers are greyed with their reason.
import type { ReactNode } from "react";
import { STEPS, stepMeta, TIER_COLOR } from "../api";
import type { ChatResponse, RequestSummary, TraceRecord } from "../api";
import { TIER_HEX, SIGNAL_HEX } from "../tokens";
import type { Tier } from "../tokens";
import { Panel, StatTile, TierDot, formatUsd, formatUsdPrecise, formatMs, formatPct } from "../components/common";

const PROVIDERS: { key: string; label: string }[] = [
  { key: "onprem", label: "On-Prem" },
  { key: "azure", label: "Azure" },
  { key: "bedrock", label: "Bedrock" },
];

function displayOf(r: TraceRecord): string {
  return r.chosen_display_name ?? r.chosen ?? "—";
}

function stepTitle(key: string): string {
  return stepMeta(key)?.label ?? key;
}

// The router's `reason` is always "tier <tier> (target <target>)" for a
// successful pick (app/routing/router.py) — the only place the chosen
// candidate's own tier reaches the UI, since TraceRecord doesn't carry it
// directly (unlike RejectedCandidate.tier). Parsed defensively: falls back to
// undefined (renders as the neutral/dimmed grey) for any other shape, e.g. a
// gateway escalation record.
function chosenTier(reason: string | undefined): Tier | undefined {
  const m = reason?.match(/^tier\s+(cheap|mid|frontier)\b/);
  return m ? (m[1] as Tier) : undefined;
}

// Compact right-aligned labels for the rejected-spur reason. Sourced from the
// exact strings app/routing/router.py emits; unknown reasons pass through
// unchanged so a future backend reason never disappears from the UI.
const REASON_LABEL: Record<string, string> = {
  "not permitted for application": "excluded · app policy",
  "disabled by admin": "disabled by admin",
  "below target tier": "below target tier",
  "above target tier": "above target tier",
  "over budget": "over budget",
  "lower score": "lower score",
};

function compactReason(reason: string): string {
  return REASON_LABEL[reason] ?? reason;
}

// One labelled row of detail — lifted from DecisionInspector.
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-sm py-1 border-b border-edge/60 last:border-0">
      <span className="text-xs text-gray-500 shrink-0">{label}</span>
      <span className="text-gray-200 text-right">{children}</span>
    </div>
  );
}

// The junction: one router/agent-hop decision rendered as a track switch —
// the chosen candidate is the LIT path (tier-coloured left rail + a glowing
// switch dot), each rejected candidate is a DIMMED spur labelled with why it
// lost. This is the demo's "legible on a projector" moment (demo-ui skill) —
// the reason list is the point, not decoration.
// Lifted from the old DecisionInspector's chosen/rejected card, extended with
// an optional running total so multi-hop requests show cumulative spend
// alongside each hop's own cost.
function Chosen({ r, cumulative }: { r: TraceRecord; cumulative?: number }) {
  const tier = chosenTier(r.reason);
  const litColor = tier ? TIER_HEX[tier] : NEUTRAL;
  return (
    <div className="relative overflow-hidden rounded-md border border-edge bg-ink/40 p-2.5 pl-3.5">
      {/* the lit rail — this candidate is the path the switch actually threw to */}
      <span aria-hidden="true" className="absolute inset-y-0 left-0 w-1" style={{ backgroundColor: litColor }} />
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className="h-2 w-2 shrink-0 rounded-full animate-rail-pulse"
          style={{ backgroundColor: litColor, boxShadow: `0 0 6px ${litColor}` }}
        />
        <span className="text-xs text-gray-500">chosen</span>
        <span className="font-semibold" style={{ color: litColor }}>{displayOf(r)}</span>
        {r.hop_index !== undefined && r.step === "agent_hop" && (
          <span className="ml-auto text-[10px] text-gray-500">hop #{r.hop_index}</span>
        )}
      </div>
      {r.reason && <div className="mt-1 text-xs text-gray-400">{r.reason}</div>}
      {r.rejected && r.rejected.length > 0 && (
        <div className="mt-2">
          <div className="uppercase tracking-wide text-[10px] text-gray-600 mb-0.5">rejected spurs</div>
          <ul className="flex flex-col gap-1">
            {r.rejected.map((c, j) => (
              <li
                key={j}
                className="flex items-center justify-between gap-2 rounded bg-ink/40 px-2 py-1 text-xs opacity-60"
              >
                <span className="flex min-w-0 items-center gap-1.5">
                  <TierDot tier={c.tier} />
                  <span className="truncate text-gray-300">{c.display_name ?? c.name}</span>
                </span>
                <span className="shrink-0 text-right text-gray-500" title={c.reason}>
                  {compactReason(c.reason)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="mt-2 flex items-center gap-3 text-[11px] text-gray-500">
        {r.actual_cost_usd != null && <span>cost {formatUsdPrecise(r.actual_cost_usd)}</span>}
        {r.est_cost_usd != null && <span>est {formatUsd(r.est_cost_usd)}</span>}
        {r.latency_ms != null && r.latency_ms > 0 && <span>{formatMs(r.latency_ms)}</span>}
        {r.tokens_priced ? <span>{r.tokens_priced} tok priced</span> : null}
        {cumulative != null && <span className="text-gray-400">cumulative {formatUsd(cumulative)}</span>}
      </div>
    </div>
  );
}

// Per-step technical detail, lifted from DecisionInspector's StepDetail
// switch (resolving the six "inferred" steps that emit no trace record from
// policy/usage/cost) plus CompressionView's fuller tokens/CCR rendering.
// Rendered non-interactively here — the drill-down shows every step at once,
// there's no click-to-focus state.
function StepBody({ stepKey, detail, hops }: { stepKey: string; detail: ChatResponse; hops: number }) {
  const t = detail.trace;
  const routerRecs = t.filter((r) => r.step === "router" || r.step === "agent_hop");
  const compRecs = t.filter((r) => r.step === "compression");
  const gatewayRecs = t.filter((r) => r.step === "gateway");
  const verify = t.find((r) => r.step === "verify");
  const p = detail.policy;

  switch (stepKey) {
    case "request":
      return (
        <>
          <Row label="request id">{detail.id}</Row>
          <Row label="served model">{detail.model_display ?? detail.model}</Row>
          <p className="text-xs text-gray-500 pt-2">
            The raw prompt is never stored or shown — nothing external sees it until the gate clears it.
          </p>
        </>
      );
    case "gate":
      return (
        <>
          <Row label="sensitivity">{p.sensitivity}</Row>
          <Row label="onprem_only">{String(p.onprem_only)}</Row>
          <Row label="detected (types)">
            {p.detected_entities.length ? (
              <span className="flex flex-wrap gap-1 justify-end">
                {p.detected_entities.map((e) => (
                  <span key={e} className="rounded bg-frontier/20 text-frontier px-1.5 py-0.5 text-xs font-semibold">
                    {e}
                  </span>
                ))}
              </span>
            ) : (
              "none"
            )}
          </Row>
          <p className="text-xs text-gray-500 pt-2">
            PII/PHI is detected and redacted here, before any model, tool, or telemetry sees it.
          </p>
        </>
      );
    case "context":
      return (
        <>
          <Row label="allowed providers">{p.allowed_providers.join(", ") || "none"}</Row>
          <Row label="onprem_only">{String(p.onprem_only)}</Row>
          <Row label="sensitivity">{p.sensitivity}</Row>
          <p className="text-xs text-gray-500 pt-2">
            This immutable policy travels with the request — the router can only pick from the allowed providers.
          </p>
        </>
      );
    case "task_mode":
      return (
        <>
          <Row label="mode">{hops > 1 ? `multi-step agent (${hops} hops)` : "direct answer"}</Row>
          <p className="text-xs text-gray-500 pt-2">
            {hops > 1
              ? "Each agent hop re-enters compression and the router — routing is per call, not per request."
              : "A single model call answers directly; no agent hops."}
          </p>
        </>
      );
    case "compression": {
      if (compRecs.length === 0) {
        return <p className="text-sm text-gray-500">Compression off / not applicable for this request.</p>;
      }
      const ccrStoredCount = compRecs.filter((c) => c.ccr_stored).length;
      const ccrRetrievals = compRecs.reduce((n, c) => n + (c.ccr_retrievals ?? 0), 0);
      const ccrToCloud = compRecs.reduce((n, c) => n + (c.ccr_retrievals_to_cloud ?? 0), 0);
      return (
        <div className="flex flex-col gap-2">
          {compRecs.map((c, i) => {
            const bypassed = c.applied === false || Boolean(c.bypass_reason);
            return (
              <div key={i} className="rounded-md border border-edge bg-ink/40 p-2.5">
                {bypassed ? (
                  <Row label={compRecs.length > 1 ? `hop ${i}` : "status"}>
                    bypassed — {c.bypass_reason ?? "not applied"}
                  </Row>
                ) : (
                  <>
                    <div className="flex items-baseline gap-3">
                      <span className="text-sm text-gray-400">{c.tokens_before ?? "–"}</span>
                      <span className="text-gray-600">→</span>
                      <span className="text-sm text-gray-200">{c.tokens_after ?? "–"}</span>
                      <span className="text-lg font-bold text-cheap ml-auto">{formatPct(c.reduction_pct)}</span>
                    </div>
                    <p className="text-xs text-gray-500 mt-0.5">via {c.compressor ?? "unknown compressor"}</p>
                  </>
                )}
              </div>
            );
          })}
          <div className="grid grid-cols-3 gap-2">
            <StatTile label="CCR stored" value={String(ccrStoredCount)} />
            <StatTile label="CCR retrievals" value={String(ccrRetrievals)} />
            <StatTile label="→ cloud" value={String(ccrToCloud)} />
          </div>
        </div>
      );
    }
    case "router": {
      if (routerRecs.length === 0) {
        return <p className="text-sm text-gray-500">No routing decision recorded.</p>;
      }
      let cumulative = 0;
      return (
        <div className="flex flex-col gap-2">
          {routerRecs.map((r, i) => {
            cumulative += r.actual_cost_usd ?? r.est_cost_usd ?? 0;
            return <Chosen key={i} r={r} cumulative={routerRecs.length > 1 ? cumulative : undefined} />;
          })}
        </div>
      );
    }
    case "gateway":
      return gatewayRecs.length === 0 ? (
        <p className="text-sm text-gray-500">No fallback — the first chosen backend succeeded.</p>
      ) : (
        <div className="flex flex-col gap-2">
          {gatewayRecs.map((r, i) => (
            <Row key={i} label="fallback">
              {r.reason}
            </Row>
          ))}
        </div>
      );
    case "backend":
      return (
        <>
          <Row label="served model">{detail.model_display ?? detail.model}</Row>
          {routerRecs[0]?.latency_ms ? <Row label="latency">{formatMs(routerRecs[0].latency_ms)}</Row> : null}
          <Row label="tokens">
            {detail.usage.prompt_tokens} in / {detail.usage.completion_tokens} out
          </Row>
          <p className="text-xs text-gray-500 pt-2">
            Runs on the chosen serving path (mock, vLLM, or Dynamo). Cost &amp; latency are folded into the router
            decision.
          </p>
        </>
      );
    case "verify":
      return !verify ? (
        <p className="text-sm text-gray-500">No verification record.</p>
      ) : (
        <>
          <Row label="required">{String(verify.required)}</Row>
          <Row label="performed">{String(verify.performed)}</Row>
          <Row label="passed">{String(verify.passed)}</Row>
          {verify.escalated !== undefined && <Row label="escalated">{String(verify.escalated)}</Row>}
          {verify.entities_found && verify.entities_found.length > 0 && (
            <Row label="found (types)">{verify.entities_found.join(", ")}</Row>
          )}
          {verify.reason && <Row label="note">{verify.reason}</Row>}
          <p className="text-xs text-gray-500 pt-2">
            The complete response is checked for leakage on the way out — redaction at egress, not just intake.
          </p>
        </>
      );
    case "response":
      return (
        <>
          <Row label="tokens">{detail.usage.total_tokens} total</Row>
          <Row label="actual spend">{formatUsdPrecise(detail.cost.total_usd)}</Row>
          <Row label="all-frontier baseline">{formatUsd(detail.cost.baseline_usd)}</Row>
          <Row label="routing saved">{formatUsd(detail.cost.routing_saved_usd)}</Row>
          <Row label="compression saved">{formatUsd(detail.cost.compression_saved_usd)}</Row>
          <div className="pt-2 text-xs text-gray-400 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
            {detail.choices[0]?.message.content}
          </div>
        </>
      );
    default:
      return <p className="text-sm text-gray-500">No detail for this step.</p>;
  }
}

// The privacy visual — greyed excluded providers with reason. Lifted from
// FlowVisualizer.
function ProviderStrip({ allowedProviders, onpremOnly }: { allowedProviders: string[]; onpremOnly: boolean }) {
  const excludedReason = onpremOnly ? "excluded · residency" : "provider off";
  return (
    <div>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Serving providers</h3>
      <div className="grid grid-cols-3 gap-2">
        {PROVIDERS.map((p) => {
          const active = allowedProviders.includes(p.key);
          return (
            <div
              key={p.key}
              className={`rounded-md border px-3 py-2 text-sm ${
                active ? "border-cheap/60 bg-cheap/10 text-gray-100" : "border-edge bg-ink/40 text-gray-600"
              }`}
              title={active ? "eligible" : excludedReason}
            >
              <div className="font-semibold">{p.label}</div>
              <div className="text-[11px]">{active ? "eligible" : excludedReason}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Verdict colours — sourced from tokens.ts, the single palette owner (last of
// the three tier-hex copies to migrate; api.ts/TIER_COLOR and BrandLogo did
// theirs already). Tier colours (cheap/mid/frontier) are reused from
// TIER_COLOR so the Model card matches the pie and dots everywhere else.
// Privacy and verification are orthogonal to tiers, so they get their own
// semantics:
//   CLEAR (cheap green)   = all-clear (cloud-eligible / verified clean)
//   SHIELD (privacy hex)  = specially handled but fine (sensitive data kept on-prem)
//   ALARM (alert hex)     = needs attention (verification flagged) — RESERVED
//                           for breach/health-fail states, never decoration
// SHIELD is a deliberate choice: "kept on-prem" is the privacy WIN, not an
// error, so it must read as neither the green all-clear nor the red alarm.
// NEUTRAL is a plain gray, not part of the semantic tier/signal palette, so
// it stays a literal — it's the "nothing to report" state (e.g. "not checked").
const CLEAR = TIER_HEX.cheap;
const SHIELD = SIGNAL_HEX.privacy;
const ALARM = SIGNAL_HEX.alert;
const NEUTRAL = "#6b7280";

// Small stroke icons (currentColor), so a card's accent colour flows straight
// through. Kept minimal — one recognisable glyph per verdict, nothing more.
const svg = "h-4 w-4 shrink-0";
const svgProps = {
  viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2,
  strokeLinecap: "round" as const, strokeLinejoin: "round" as const, className: svg,
};
const IconShield = () => (<svg {...svgProps}><path d="M12 3l7 3v5c0 4.6-3 7.7-7 9-4-1.3-7-4.4-7-9V6l7-3z" /></svg>);
const IconChip = () => (
  <svg {...svgProps}>
    <rect x="7" y="7" width="10" height="10" rx="1.5" />
    <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" />
  </svg>
);
const IconCheck = () => (<svg {...svgProps}><circle cx="12" cy="12" r="9" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></svg>);
const IconFlag = () => (<svg {...svgProps}><path d="M12 3l9 16H3l9-16z" /><path d="M12 10v4M12 17h.01" /></svg>);
const IconDash = () => (<svg {...svgProps}><circle cx="12" cy="12" r="9" /><path d="M8 12h8" /></svg>);

// A "story" card: icon + label in the accent colour, a bold verdict, and one
// plain-language line saying WHY. The left accent bar carries the verdict's
// meaning at a glance.
function OutcomeCard({ icon, accent, label, verdict, why, children }: {
  icon: ReactNode; accent: string; label: string; verdict: ReactNode; why: string; children?: ReactNode;
}) {
  return (
    <div className="relative overflow-hidden rounded-lg border border-edge bg-ink/40 p-3 pl-4">
      <span className="absolute inset-y-0 left-0 w-1" style={{ backgroundColor: accent }} />
      <div className="flex items-center gap-1.5" style={{ color: accent }}>
        {icon}
        <span className="text-[11px] font-semibold uppercase tracking-wide">{label}</span>
      </div>
      <div className="mt-1 text-base font-semibold leading-tight text-gray-50">{verdict}</div>
      <p className="mt-0.5 text-xs leading-snug text-gray-400">{why}</p>
      {children}
    </div>
  );
}

// A compact fact chip. `accent` colours the label icon + value (used for the
// verification verdict); cost/latency pass none and stay neutral.
function MetricChip({ icon, accent, label, value, sub }: {
  icon?: ReactNode; accent?: string; label: string; value: string; sub?: string;
}) {
  return (
    <div className="rounded-lg border border-edge bg-ink/40 px-3 py-2">
      <div className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-gray-500">
        {icon && <span style={{ color: accent }}>{icon}</span>}
        {label}
      </div>
      <div className="mt-0.5 text-lg font-bold leading-tight" style={{ color: accent ?? "#f3f4f6" }}>{value}</div>
      {sub && <div className="text-[11px] text-gray-500 truncate">{sub}</div>}
    </div>
  );
}

// Band 1 — owner-facing summary as labelled outcome cards. No step numbers, no
// jargon, no run-on sentence: each card answers one question an app owner asks
// ("was it private? which model, and why? was the output clean? what did it
// cost?"). `summary` carries the already-computed tier so the Model card can
// name it; detail is the source of truth for everything else.
function PlainSummary({ detail, summary }: { detail: ChatResponse; summary: RequestSummary | null }) {
  const p = detail.policy;
  const entities = p.detected_entities;
  const verify = detail.trace.find((r) => r.step === "verify");
  const modelDisplay = summary?.model_display ?? detail.model_display ?? detail.model;
  const tier = summary?.tier ?? "?";
  const hops =
    summary?.hops ??
    Math.max(detail.trace.filter((r) => r.step === "router" || r.step === "agent_hop").length, 1);
  const totalLatencyMs =
    summary?.latency_ms ?? detail.trace.reduce((sum, r) => sum + (r.latency_ms ?? 0), 0);

  // Privacy — derived from the EFFECTIVE candidate pool, not onprem_only alone.
  // onprem_only=false only means residency didn't force on-prem; it does NOT mean
  // cloud is reachable (the kill-switch may have disabled it). Read allowed_providers.
  const hasCloud = p.allowed_providers.some((x) => x !== "onprem" && x !== "mock");
  const hasEntities = entities.length > 0;
  const entityList = entities.join(", ");
  let privacyAccent: string;
  let privacyVerdict: string;
  let privacyWhy: string;
  if (p.onprem_only) {
    privacyAccent = SHIELD;
    privacyVerdict = "Kept on-prem";
    privacyWhy = hasEntities
      ? `Detected ${entityList} — residency requires on-prem serving; cloud was ineligible.`
      : "Residency policy requires on-prem serving; cloud was ineligible.";
  } else if (!hasCloud) {
    privacyAccent = NEUTRAL;
    privacyVerdict = "On-prem only";
    privacyWhy = "Cloud providers are disabled — this request was served on-prem.";
  } else {
    privacyAccent = CLEAR;
    privacyVerdict = "Cloud-eligible";
    privacyWhy = hasEntities
      ? `Detected ${entityList}, but none require on-prem residency — every provider was eligible.`
      : "No PII detected — every provider was eligible.";
  }

  // Model — states WHY this model won, so "cloud-eligible but ran local" stops
  // looking contradictory. Truthful to the router: target tier = max(task
  // complexity, risk floor), then the cheapest model in that tier wins (on-prem
  // is cheaper, so it usually does).
  const tierLabel = tier === "?" ? "" : `${tier} tier`;
  const modelWhy =
    hops > 1
      ? `${hops} hops, each routed on its own — final answer from this model.`
      : p.onprem_only
        ? `Cheapest qualifying model in the ${tier} tier, kept on-prem.`
        : `Cheapest model in the ${tier} tier — the lowest tier that met this task's risk bar.`;

  // Verification — egress leakage check.
  const vState = !verify ? "none" : verify.passed ? "ok" : "flag";
  const verify_ = {
    ok: { accent: CLEAR, icon: <IconCheck />, verdict: "Clean", sub: "checked on egress" },
    flag: { accent: ALARM, icon: <IconFlag />, verdict: "Flagged", sub: "held for review" },
    none: { accent: NEUTRAL, icon: <IconDash />, verdict: "Not checked", sub: "not required" },
  }[vState];

  return (
    <Panel title="What happened">
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        <OutcomeCard icon={<IconShield />} accent={privacyAccent} label="Privacy"
          verdict={privacyVerdict} why={privacyWhy}>
          {entities.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {entities.map((e) => (
                <span key={e} className="rounded px-1.5 py-0.5 text-[11px] font-semibold"
                  style={{ backgroundColor: `${privacyAccent}22`, color: privacyAccent }}>
                  {e}
                </span>
              ))}
            </div>
          )}
        </OutcomeCard>
        <OutcomeCard icon={<IconChip />} accent={TIER_COLOR[tier] ?? NEUTRAL} label="Model"
          verdict={
            <span className="flex items-center gap-2">
              <TierDot tier={tier} />
              <span className="truncate">{modelDisplay}</span>
              {tierLabel && <span className="text-xs font-normal text-gray-500">{tierLabel}</span>}
            </span>
          }
          why={modelWhy} />
      </div>

      <div className="grid grid-cols-3 gap-2.5">
        <MetricChip icon={verify_.icon} accent={verify_.accent} label="Verified"
          value={verify_.verdict} sub={verify_.sub} />
        <MetricChip label="Cost" value={formatUsdPrecise(detail.cost.total_usd)}
          sub={`vs ${formatUsd(detail.cost.baseline_usd)} all-frontier`} />
        <MetricChip label="Latency" value={formatMs(totalLatencyMs)}
          sub={hops > 1 ? `${hops} hops` : "end to end"} />
      </div>

      <ProviderStrip allowedProviders={p.allowed_providers} onpremOnly={p.onprem_only} />
    </Panel>
  );
}

// Band 2 — full admin-facing technical trace: every step's detail in order,
// plus the live Langfuse deep-link.
function TechnicalTrace({ detail }: { detail: ChatResponse }) {
  const routerRecs = detail.trace.filter((r) => r.step === "router" || r.step === "agent_hop");
  const hops = Math.max(routerRecs.length, 1);
  const lfUrl = detail.langfuse_url;

  return (
    <Panel
      title="Technical trace"
      right={
        lfUrl ? (
          <div className="flex flex-col items-end gap-0.5">
            <a
              href={lfUrl}
              target="_blank"
              rel="noreferrer"
              title="Opens Langfuse — sign in as demo@example.com / demodemo the first time"
              className="text-xs text-accent hover:underline"
            >
              Langfuse trace ↗
            </a>
            <span className="text-[10px] text-gray-600">sign in: demo@example.com / demodemo</span>
          </div>
        ) : (
          <span className="text-xs text-gray-600" title="telemetry disabled — no LANGFUSE keys set">
            Langfuse off
          </span>
        )
      }
    >
      {hops > 1 && (
        <p className="text-xs text-gray-500 -mt-1">
          Multi-step request — {hops} agent hops, each re-entering compression and the router.
        </p>
      )}
      <div className="flex flex-col gap-3 max-h-[560px] overflow-y-auto pr-1">
        {STEPS.map((s) => (
          <div key={s.key} className="rounded-md border border-edge/70 bg-panel p-3">
            <div className="mb-2">
              <div className="text-sm font-semibold text-gray-100">{stepTitle(s.key)}</div>
              <div className="text-xs text-gray-500">{s.caption}</div>
            </div>
            <StepBody stepKey={s.key} detail={detail} hops={hops} />
          </div>
        ))}
      </div>
    </Panel>
  );
}

export function RequestDrilldown(
  { detail, summary = null, loading }: { detail: ChatResponse | null; summary?: RequestSummary | null; loading: boolean },
) {
  if (loading) {
    return (
      <Panel title="Request">
        <p className="text-sm text-gray-500">Loading…</p>
      </Panel>
    );
  }
  if (!detail) {
    return (
      <Panel title="Request">
        <p className="text-sm text-gray-500">Select a request to see its journey.</p>
      </Panel>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PlainSummary detail={detail} summary={summary} />
      <TechnicalTrace detail={detail} />
    </div>
  );
}
