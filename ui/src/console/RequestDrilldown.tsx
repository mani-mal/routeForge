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
import { STEPS, stepMeta } from "../api";
import type { ChatResponse, TraceRecord } from "../api";
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

// One labelled row of detail — lifted from DecisionInspector.
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-sm py-1 border-b border-edge/60 last:border-0">
      <span className="text-xs text-gray-500 shrink-0">{label}</span>
      <span className="text-gray-200 text-right">{children}</span>
    </div>
  );
}

// Chosen-vs-rejected candidate card — lifted from DecisionInspector, extended
// with an optional running total so multi-hop requests show cumulative spend
// alongside each hop's own cost.
function Chosen({ r, cumulative }: { r: TraceRecord; cumulative?: number }) {
  return (
    <div className="rounded-md border border-edge bg-ink/40 p-2.5">
      <div className="flex items-center gap-2">
        <span className="text-xs text-gray-500">chosen</span>
        <span className="font-semibold text-cheap">{displayOf(r)}</span>
        {r.hop_index !== undefined && r.step === "agent_hop" && (
          <span className="ml-auto text-[10px] text-gray-500">hop #{r.hop_index}</span>
        )}
      </div>
      {r.reason && <div className="mt-1 text-xs text-gray-400">{r.reason}</div>}
      {r.rejected && r.rejected.length > 0 && (
        <div className="mt-2">
          <div className="uppercase tracking-wide text-[10px] text-gray-600 mb-0.5">rejected</div>
          <ul className="flex flex-col gap-0.5">
            {r.rejected.map((c, j) => (
              <li key={j} className="flex items-center gap-1.5 text-xs">
                <TierDot tier={c.tier} />
                <span className="text-gray-300">{c.display_name ?? c.name}</span>
                <span className="text-gray-500">— {c.reason}</span>
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
  const excludedReason = onpremOnly ? "excluded — residency" : "excluded by policy";
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

// Band 1 — plain-language, owner-facing summary. No step numbers, no jargon.
function PlainSummary({ detail }: { detail: ChatResponse }) {
  const p = detail.policy;
  const entities = p.detected_entities;
  const verify = detail.trace.find((r) => r.step === "verify");
  const modelDisplay = detail.model_display ?? detail.model;
  const totalLatencyMs = detail.trace.reduce((sum, r) => sum + (r.latency_ms ?? 0), 0);

  return (
    <Panel title="What happened">
      <p className="text-base leading-relaxed text-gray-100">
        Checked for sensitive data (
        <span className="font-semibold text-gray-50">
          {entities.length ? `found ${entities.join(", ")}` : "none"}
        </span>
        ) → <span className="font-semibold text-gray-50">{p.onprem_only ? "kept on your infrastructure" : "eligible for cloud"}</span> →
        answered by <span className="font-semibold text-gray-50">{modelDisplay}</span> →{" "}
        <span className="font-semibold text-gray-50">
          {verify ? (verify.passed ? "verified clean" : "flagged for review") : "not verified"}
        </span>
        , in {formatMs(totalLatencyMs)} for {formatUsdPrecise(detail.cost.total_usd)}.
      </p>
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

export function RequestDrilldown({ detail, loading }: { detail: ChatResponse | null; loading: boolean }) {
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
      <PlainSummary detail={detail} />
      <TechnicalTrace detail={detail} />
    </div>
  );
}
