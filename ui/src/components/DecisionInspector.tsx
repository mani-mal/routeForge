// Panel 2 — the legibility panel, now step-aware. Click a node in the flow
// visualizer and this focuses that step's detail for the selected request —
// including the six "inferred" steps that emit no record (their data lives on
// the policy / usage / cost blocks). With no step selected it shows the whole
// decision trace. Entity TYPES only (never values); model names are the
// human-facing display names; the Langfuse link is a live deep-link.
import type { ReactNode } from "react";
import { stepMeta } from "../api";
import type { ChatResponse, TraceRecord } from "../api";
import { Panel, TierDot, formatUsd, formatMs, formatPct } from "./common";

function displayOf(r: TraceRecord): string {
  return r.chosen_display_name ?? r.chosen ?? "—";
}

// One labelled row of detail.
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-sm py-1 border-b border-edge/60 last:border-0">
      <span className="text-xs text-gray-500 shrink-0">{label}</span>
      <span className="text-gray-200 text-right">{children}</span>
    </div>
  );
}

function Chosen({ r }: { r: TraceRecord }) {
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
        {r.actual_cost_usd != null && <span>cost {formatUsd(r.actual_cost_usd)}</span>}
        {r.est_cost_usd != null && <span>est {formatUsd(r.est_cost_usd)}</span>}
        {r.latency_ms != null && r.latency_ms > 0 && <span>{formatMs(r.latency_ms)}</span>}
        {r.tokens_priced ? <span>{r.tokens_priced} tok priced</span> : null}
      </div>
    </div>
  );
}

// Focused detail for one of the ten steps, resolving inferred steps from the
// response's policy / usage / cost blocks.
function StepDetail({ stepKey, detail, hops }: { stepKey: string; detail: ChatResponse; hops: number }) {
  const meta = stepMeta(stepKey);
  const t = detail.trace;
  const routerRecs = t.filter((r) => r.step === "router" || r.step === "agent_hop");
  const compRecs = t.filter((r) => r.step === "compression");
  const gatewayRecs = t.filter((r) => r.step === "gateway");
  const verify = t.find((r) => r.step === "verify");
  const p = detail.policy;

  let body: React.ReactNode;
  switch (stepKey) {
    case "request":
      body = (
        <>
          <Row label="request id">{detail.id}</Row>
          <Row label="served model">{detail.model_display ?? detail.model}</Row>
          <p className="text-xs text-gray-500 pt-2">
            The raw prompt is never stored or shown — nothing external sees it until the gate clears it.
          </p>
        </>
      );
      break;
    case "gate":
      body = (
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
      break;
    case "context":
      body = (
        <>
          <Row label="allowed providers">{p.allowed_providers.join(", ") || "none"}</Row>
          <Row label="onprem_only">{String(p.onprem_only)}</Row>
          <Row label="sensitivity">{p.sensitivity}</Row>
          <p className="text-xs text-gray-500 pt-2">
            This immutable policy travels with the request — the router can only pick from the allowed providers.
          </p>
        </>
      );
      break;
    case "task_mode":
      body = (
        <>
          <Row label="mode">{hops > 1 ? `multi-step agent (${hops} hops)` : "direct answer"}</Row>
          <p className="text-xs text-gray-500 pt-2">
            {hops > 1
              ? "Each agent hop re-enters compression and the router — routing is per call, not per request."
              : "A single model call answers directly; no agent hops."}
          </p>
        </>
      );
      break;
    case "compression":
      body = compRecs.length === 0 ? (
        <p className="text-sm text-gray-500">Compression off / not applicable for this request.</p>
      ) : (
        <>
          {compRecs.map((c, i) => {
            const bypassed = c.applied === false || Boolean(c.bypass_reason);
            return (
              <div key={i} className="py-1">
                {bypassed ? (
                  <Row label={compRecs.length > 1 ? `hop ${i}` : "status"}>
                    bypassed — {c.bypass_reason ?? "not applied"}
                  </Row>
                ) : (
                  <Row label={compRecs.length > 1 ? `hop ${i}` : "tokens"}>
                    {c.tokens_before} → {c.tokens_after} ({formatPct(c.reduction_pct)} via {c.compressor ?? "?"})
                  </Row>
                )}
              </div>
            );
          })}
          <Row label="CCR → cloud">{compRecs.reduce((n, c) => n + (c.ccr_retrievals_to_cloud ?? 0), 0)}</Row>
        </>
      );
      break;
    case "router":
      body = routerRecs.length === 0 ? (
        <p className="text-sm text-gray-500">No routing decision recorded.</p>
      ) : (
        <div className="flex flex-col gap-2">
          {routerRecs.map((r, i) => (
            <Chosen key={i} r={r} />
          ))}
        </div>
      );
      break;
    case "gateway":
      body =
        gatewayRecs.length === 0 ? (
          <p className="text-sm text-gray-500">No fallback — the first chosen backend succeeded.</p>
        ) : (
          <div className="flex flex-col gap-2">
            {gatewayRecs.map((r, i) => (
              <Row key={i} label="fallback">{r.reason}</Row>
            ))}
          </div>
        );
      break;
    case "backend":
      body = (
        <>
          <Row label="served model">{detail.model_display ?? detail.model}</Row>
          {routerRecs[0]?.latency_ms ? <Row label="latency">{formatMs(routerRecs[0].latency_ms)}</Row> : null}
          <Row label="tokens">{detail.usage.prompt_tokens} in / {detail.usage.completion_tokens} out</Row>
          <p className="text-xs text-gray-500 pt-2">
            Runs on the chosen serving path (mock, vLLM, or Dynamo). Cost & latency are folded into the router decision.
          </p>
        </>
      );
      break;
    case "verify":
      body = !verify ? (
        <p className="text-sm text-gray-500">No verification record.</p>
      ) : (
        <>
          <Row label="required">{String(verify.required)}</Row>
          <Row label="performed">{String(verify.performed)}</Row>
          <Row label="passed">{String(verify.passed)}</Row>
          {verify.entities_found && verify.entities_found.length > 0 && (
            <Row label="found (types)">{verify.entities_found.join(", ")}</Row>
          )}
          {verify.reason && <Row label="note">{verify.reason}</Row>}
          <p className="text-xs text-gray-500 pt-2">
            The complete response is checked for leakage on the way out — redaction at egress, not just intake.
          </p>
        </>
      );
      break;
    case "response":
      body = (
        <>
          <Row label="tokens">{detail.usage.total_tokens} total</Row>
          <Row label="actual spend">{formatUsd(detail.cost.total_usd)}</Row>
          <Row label="all-frontier baseline">{formatUsd(detail.cost.baseline_usd)}</Row>
          <Row label="routing saved">{formatUsd(detail.cost.routing_saved_usd)}</Row>
          <Row label="compression saved">{formatUsd(detail.cost.compression_saved_usd)}</Row>
          <div className="pt-2 text-xs text-gray-400 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
            {detail.choices[0]?.message.content}
          </div>
        </>
      );
      break;
    default:
      body = <p className="text-sm text-gray-500">No detail for this step.</p>;
  }

  return (
    <div>
      <div className="mb-2">
        <div className="text-sm font-semibold text-gray-100">{meta?.label ?? stepKey}</div>
        <div className="text-xs text-gray-500">{meta?.caption}</div>
      </div>
      {body}
    </div>
  );
}

// Fallback view: the whole decision trace as a scrollable card list.
function stepTitle(key: string): string {
  return stepMeta(key)?.label ?? key;
}

export function DecisionInspector({
  requestId,
  detail,
  loading,
  selectedStep,
  onClearStep,
  hops,
}: {
  requestId: string | null;
  detail: ChatResponse | null;
  loading: boolean;
  selectedStep: string | null;
  onClearStep: () => void;
  hops: number;
}) {
  if (!requestId) {
    return (
      <Panel title="Decision inspector">
        <p className="text-sm text-gray-500">Select a request, then click any step in the flow to inspect it.</p>
      </Panel>
    );
  }
  if (!detail) {
    return (
      <Panel title="Decision inspector">
        <p className="text-sm text-gray-500">{loading ? "Loading trace…" : "Trace not available yet — still streaming."}</p>
      </Panel>
    );
  }

  const lfUrl = detail.langfuse_url;
  let cumulative = 0;

  return (
    <Panel
      title="Decision inspector"
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
      <div className="flex flex-wrap items-center gap-2 text-xs text-gray-400">
        <span>sensitivity: <span className="text-gray-200">{detail.policy.sensitivity}</span></span>
        <span>onprem_only: <span className="text-gray-200">{String(detail.policy.onprem_only)}</span></span>
        {detail.policy.detected_entities.map((tp) => (
          <span key={tp} className="rounded bg-frontier/20 text-frontier px-1.5 py-0.5 font-semibold">
            {tp}
          </span>
        ))}
      </div>

      {selectedStep ? (
        <div>
          <button onClick={onClearStep} className="text-xs text-gray-500 hover:text-gray-300 mb-2">
            ← all steps
          </button>
          <StepDetail stepKey={selectedStep} detail={detail} hops={hops} />
        </div>
      ) : (
        <div className="flex flex-col gap-2 max-h-[420px] overflow-y-auto pr-1">
          {detail.trace.map((r, i) => {
            const cost = r.actual_cost_usd ?? r.est_cost_usd;
            if (cost) cumulative += cost;
            return (
              <div key={i} className="rounded-md border border-edge bg-ink/40 p-2.5 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold text-gray-200">{stepTitle(r.step)}</span>
                  <span className="text-xs text-gray-500">{formatMs(r.latency_ms)}</span>
                </div>
                {r.chosen && (
                  <div className="mt-1 text-xs text-gray-300">
                    chosen: <span className="font-medium text-cheap">{displayOf(r)}</span>
                    {r.reason && <span className="text-gray-500"> — {r.reason}</span>}
                  </div>
                )}
                {r.rejected && r.rejected.length > 0 && (
                  <div className="mt-1 text-xs text-gray-500">
                    <div className="uppercase tracking-wide text-[10px] text-gray-600">rejected</div>
                    <ul className="flex flex-col gap-0.5 mt-0.5">
                      {r.rejected.map((c, j) => (
                        <li key={j} className="flex items-center gap-1.5">
                          <TierDot tier={c.tier} />
                          <span className="text-gray-300">{c.display_name ?? c.name}</span>
                          <span className="text-gray-600">({c.provider})</span>
                          <span className="text-gray-500">— {c.reason}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {(r.est_cost_usd !== undefined || r.actual_cost_usd !== undefined) && (
                  <div className="mt-1 flex items-center gap-3 text-xs text-gray-500">
                    <span>est: {formatUsd(r.est_cost_usd)}</span>
                    <span>actual: {formatUsd(r.actual_cost_usd)}</span>
                    <span className="text-gray-400">cumulative: {formatUsd(cumulative)}</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
