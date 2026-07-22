// Panel 2 — the legibility panel. Step-by-step trace: chosen vs rejected
// candidates with reasons, entity TYPES only (never values), per-hop and
// cumulative cost, latency, and a Langfuse deep-link placeholder.
import { STEPS } from "../api";
import type { ChatResponse } from "../api";
import { Panel, TierDot, formatUsd, formatMs } from "./common";

function stepLabel(key: string): string {
  return STEPS.find((s) => s.key === key)?.label ?? key;
}

export function DecisionInspector({
  requestId,
  detail,
  loading,
}: {
  requestId: string | null;
  detail: ChatResponse | null;
  loading: boolean;
}) {
  if (!requestId) {
    return (
      <Panel title="Decision inspector">
        <p className="text-sm text-gray-500">Select a request to inspect its trace.</p>
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

  let cumulative = 0;

  return (
    <Panel
      title="Decision inspector"
      right={
        <a href="#" className="text-xs text-mid hover:underline">
          Langfuse trace ↗
        </a>
      }
    >
      <div className="flex flex-wrap items-center gap-2 text-xs text-gray-400">
        <span>
          sensitivity: <span className="text-gray-200">{detail.policy.sensitivity}</span>
        </span>
        <span>
          onprem_only: <span className="text-gray-200">{String(detail.policy.onprem_only)}</span>
        </span>
        {detail.policy.detected_entities.map((t) => (
          <span key={t} className="rounded bg-frontier/20 text-frontier px-1.5 py-0.5 font-semibold">
            {t}
          </span>
        ))}
      </div>

      <div className="flex flex-col gap-2 max-h-[420px] overflow-y-auto pr-1">
        {detail.trace.map((r, i) => {
          const cost = r.actual_cost_usd ?? r.est_cost_usd;
          if (cost) cumulative += cost;
          return (
            <div key={i} className="rounded-md border border-edge bg-ink/40 p-2.5 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-gray-200">{stepLabel(r.step)}</span>
                <span className="text-xs text-gray-500">{formatMs(r.latency_ms)}</span>
              </div>
              {r.chosen && (
                <div className="mt-1 text-xs text-gray-300">
                  chosen: <span className="font-medium text-cheap">{r.chosen}</span>
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
                        <span className="text-gray-300">{c.name}</span>
                        <span className="text-gray-600">({c.provider})</span>
                        <span className="text-gray-500">— {c.reason}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {r.entities_found && r.entities_found.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1 text-xs">
                  {r.entities_found.map((t) => (
                    <span key={t} className="rounded bg-frontier/20 text-frontier px-1.5 py-0.5 font-semibold">
                      {t}
                    </span>
                  ))}
                </div>
              )}
              {(r.est_cost_usd !== undefined || r.actual_cost_usd !== undefined) && (
                <div className="mt-1 flex items-center gap-3 text-xs text-gray-500">
                  <span>est: {formatUsd(r.est_cost_usd)}</span>
                  <span>actual: {formatUsd(r.actual_cost_usd)}</span>
                  <span className="text-gray-400">cumulative: {formatUsd(cumulative)}</span>
                </div>
              )}
              {r.hop_index !== undefined && (
                <div className="mt-1 text-xs text-gray-500">hop #{r.hop_index}</div>
              )}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}
