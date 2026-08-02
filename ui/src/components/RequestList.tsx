// Panel 1 (bottom half) — newest-first request list. Click a row to inspect it.
import type { RequestSummary } from "../api";
import { Panel, TierDot, formatUsdPrecise, formatMs } from "./common";

export function RequestList({
  requests,
  selectedId,
  onSelect,
}: {
  requests: RequestSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <Panel title="Requests" right={<span className="text-xs text-gray-500">{requests.length}</span>} className="flex-1 min-h-0">
      {requests.length === 0 ? (
        <p className="text-sm text-gray-500">No requests yet — they appear here as connected applications call the API.</p>
      ) : (
        <div className="flex flex-col gap-1.5 overflow-y-auto max-h-[420px] pr-1">
          {requests.map((r) => {
            const selected = r.id === selectedId;
            return (
              <button
                key={r.id}
                onClick={() => onSelect(r.id)}
                className={`text-left rounded-md border px-3 py-2 text-sm transition-colors ${
                  selected ? "border-cheap bg-cheap/10" : "border-edge bg-ink/40 hover:border-gray-600"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <TierDot tier={r.tier} />
                    <span className="font-medium truncate" title={r.model}>{r.model_display ?? r.model}</span>
                    {r.onprem_only && (
                      <span className="shrink-0 rounded bg-mid/20 text-mid text-[10px] font-semibold px-1.5 py-0.5">
                        on-prem
                      </span>
                    )}
                    {r.hops > 1 && (
                      <span className="shrink-0 rounded bg-gray-700 text-gray-300 text-[10px] font-semibold px-1.5 py-0.5">
                        {r.hops} hops
                      </span>
                    )}
                  </div>
                  <span className="shrink-0 text-xs font-mono text-gray-400">{formatUsdPrecise(r.total_usd)}</span>
                </div>
                <div className="mt-1 flex items-center justify-between text-xs text-gray-500">
                  <span>{r.sensitivity}</span>
                  <span className="font-mono">{formatMs(r.latency_ms)}</span>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
