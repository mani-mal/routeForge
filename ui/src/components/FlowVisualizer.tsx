// Panel 1 — the centrepiece. The ten serving-plane steps as an interactive,
// numbered spine. Each node shows what the step does; clicking one drives the
// step inspector to that step's detail for the selected request. Nodes are
// coloured by state: "recorded" (this step emitted a decision), "traversed"
// (the request passed through but the step emits no record), or "skipped".
// The provider chips grey out when policy excludes them — that grey state is
// the whole privacy story, so it keeps top billing.
import { STEPS } from "../api";
import type { TraceRecord } from "../api";
import { Panel } from "./common";

const PROVIDERS: { key: string; label: string }[] = [
  { key: "onprem", label: "On-Prem" },
  { key: "azure", label: "Azure" },
  { key: "bedrock", label: "Bedrock" },
];

type NodeState = "recorded" | "traversed" | "skipped";

// Map the emitted trace records onto the ten canonical nodes. Only compression,
// router (+agent_hop), verify and (on fallback) gateway emit records; the rest
// are inferred for a completed request. "recorded" nodes have data to inspect;
// "traversed" nodes are explained but carry no per-request record.
function nodeStates(trace: TraceRecord[]): Record<string, NodeState> {
  const seen = new Set(trace.map((r) => r.step));
  const generated = seen.has("router") || seen.has("agent_hop");
  const has = trace.length > 0;
  const out: Record<string, NodeState> = {};
  for (const s of STEPS) out[s.key] = "skipped";
  if (!has) return out;
  // Always traversed for a completed request:
  ["request", "gate", "context", "task_mode", "response"].forEach((k) => (out[k] = "traversed"));
  if (generated) ["router", "backend"].forEach((k) => (out[k] = "traversed"));
  // Nodes that carry an inspectable record:
  if (seen.has("compression")) out.compression = "recorded";
  if (generated) out.router = "recorded";
  if (seen.has("gateway")) out.gateway = "recorded";
  if (seen.has("verify")) out.verify = "recorded";
  return out;
}

export function FlowVisualizer({
  trace,
  allowedProviders,
  onpremOnly,
  hops,
  selectedStep,
  onSelectStep,
}: {
  trace: TraceRecord[];
  allowedProviders: string[] | null;
  onpremOnly: boolean | null;
  hops: number;
  selectedStep: string | null;
  onSelectStep: (key: string) => void;
}) {
  const hasActivity = trace.length > 0;
  const states = nodeStates(trace);
  const excludedReason = onpremOnly ? "excluded — residency" : "excluded by policy";

  return (
    <Panel
      title="Flow visualizer"
      right={
        <span className="text-[11px] text-gray-500">
          {hasActivity ? "click a step for detail" : "10-step serving plane"}
        </span>
      }
    >
      {!hasActivity && (
        <p className="text-sm text-gray-500">
          No active request — fire one from the control panel to watch it flow through the ten steps.
        </p>
      )}

      <ol className="grid grid-cols-2 sm:grid-cols-5 gap-1.5">
        {STEPS.map((s, i) => {
          const state = states[s.key];
          const selected = selectedStep === s.key;
          const isRouter = s.key === "router";
          const dotClass =
            state === "recorded" ? "bg-cheap" : state === "traversed" ? "bg-accent/70" : "bg-gray-700";
          const frame = selected
            ? "border-accent bg-accent/10 ring-1 ring-accent"
            : state === "skipped"
            ? "border-edge bg-ink/40 hover:border-gray-600"
            : "border-edge bg-panel hover:border-gray-500";
          const numClass = state === "skipped" ? "text-gray-600" : "text-gray-400";
          const nameClass = state === "skipped" ? "text-gray-500" : "text-gray-100";
          return (
            <li key={s.key}>
              <button
                onClick={() => onSelectStep(s.key)}
                aria-pressed={selected}
                title={s.caption}
                className={`relative w-full h-full text-left rounded-md border px-2 py-1.5 transition-colors ${frame}`}
              >
                <div className="flex items-center gap-1.5">
                  <span className={`tabular-nums text-[10px] font-semibold ${numClass}`}>
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className={`h-1.5 w-1.5 rounded-full ${dotClass}`} />
                  {isRouter && hops > 1 && (
                    <span className="ml-auto rounded-full bg-mid text-white text-[9px] px-1 leading-tight">
                      ↻×{hops}
                    </span>
                  )}
                </div>
                <div className={`mt-0.5 text-[11px] font-semibold leading-tight ${nameClass}`}>
                  {s.label}
                </div>
                <div className="text-[9.5px] text-gray-500 leading-tight mt-0.5 line-clamp-2">
                  {s.caption}
                </div>
              </button>
            </li>
          );
        })}
      </ol>

      {hasActivity && (
        <div className="flex items-center gap-3 text-[10px] text-gray-500">
          <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-cheap" /> recorded</span>
          <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-accent/70" /> traversed</span>
          <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-gray-700" /> skipped</span>
        </div>
      )}

      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Serving providers</h3>
        <div className="grid grid-cols-3 gap-2">
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
