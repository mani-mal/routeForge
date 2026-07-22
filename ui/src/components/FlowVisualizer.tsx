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
  const taken = new Set(trace.map((r) => r.step));
  const hasActivity = trace.length > 0;
  const excludedReason = onpremOnly ? "excluded by policy (residency)" : "excluded by policy";

  return (
    <Panel title="Flow visualizer" right={hasActivity ? <span className="text-xs text-gray-500">{trace.length} steps recorded</span> : undefined}>
      <div className="flex flex-wrap items-center gap-1">
        {STEPS.map((s, i) => {
          const lit = taken.has(s.key);
          const isRouter = s.key === "router";
          return (
            <div key={s.key} className="flex items-center gap-1">
              <div
                className={`relative rounded-md border px-2 py-1.5 text-[11px] font-medium leading-tight text-center min-w-[74px] ${
                  lit ? "border-cheap bg-cheap/10 text-gray-100" : "border-edge bg-ink/40 text-gray-500"
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
