// Panel 1 — the centrepiece. Renders the ten serving-plane steps as a node
// chain and lights the ones the selected/live request actually took, plus
// the three provider chips greyed out when policy excludes them. That grey
// state is the whole privacy story, so it gets top billing here.
import { STEPS } from "../api";
import type { TraceRecord } from "../api";
import { Panel } from "./common";

const PROVIDERS: { key: string; label: string }[] = [
  { key: "onprem", label: "On-Prem" },
  { key: "azure", label: "Azure" },
  { key: "bedrock", label: "Bedrock" },
];

export function FlowVisualizer({
  trace,
  allowedProviders,
  onpremOnly,
  hops,
}: {
  trace: TraceRecord[];
  allowedProviders: string[] | null;
  onpremOnly: boolean | null;
  hops: number;
}) {
  const hasActivity = trace.length > 0;
  // The trace only carries records for steps that emit one (router/agent_hop,
  // gateway on fallback, compression, verify). But a completed request always
  // traversed the gate, context, task-mode and returned a response — so those
  // nodes light whenever there's activity. Map the emitted records onto the
  // ten canonical nodes so the centrepiece (esp. the Policy Gate node that
  // anchors the privacy story, and the Router node on multi-step) lights up.
  const steps = new Set(trace.map((r) => r.step));
  const generated = steps.has("router") || steps.has("agent_hop"); // a model ran
  const lit = new Set<string>();
  if (hasActivity) {
    // pre-router steps + final response always happen for a completed request
    ["request", "gate", "context", "task_mode", "response"].forEach((k) => lit.add(k));
    if (steps.has("compression")) lit.add("compression");
    if (generated) ["router", "gateway", "backend"].forEach((k) => lit.add(k));
    if (steps.has("verify")) lit.add("verify");
  }
  const excludedReason = onpremOnly ? "excluded by policy (residency)" : "excluded by policy";

  return (
    <Panel title="Flow visualizer" right={hasActivity ? <span className="text-xs text-gray-500">{trace.length} steps recorded</span> : undefined}>
      <div className="flex flex-wrap items-center gap-1">
        {STEPS.map((s, i) => {
          const isLit = lit.has(s.key);
          const isRouter = s.key === "router";
          return (
            <div key={s.key} className="flex items-center gap-1">
              <div
                className={`relative rounded-md border px-2 py-1.5 text-[11px] font-medium leading-tight text-center min-w-[74px] ${
                  isLit ? "border-cheap bg-cheap/10 text-gray-100" : "border-edge bg-ink/40 text-gray-500"
                }`}
              >
                {s.label}
                {isRouter && hops > 1 && (
                  <span className="absolute -top-2 -right-2 rounded-full bg-mid text-white text-[10px] px-1.5 py-0.5 leading-none">
                    ↻ ×{hops}
                  </span>
                )}
              </div>
              {i < STEPS.length - 1 && <span className="text-gray-700">›</span>}
            </div>
          );
        })}
      </div>

      {!hasActivity && (
        <p className="text-sm text-gray-500">No active request — fire one from the control panel to watch it flow.</p>
      )}

      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Serving providers</h3>
        <div className="flex flex-wrap gap-2">
          {PROVIDERS.map((p) => {
            const active = allowedProviders === null ? true : allowedProviders.includes(p.key);
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
    </Panel>
  );
}
