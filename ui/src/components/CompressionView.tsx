// Step 5 view — tokens before/after, reduction %, which compressor fired,
// and CCR store/retrieve *counts* (never contents). Shows the bypass
// reason when compression didn't run so customers can see why.
import type { ChatResponse } from "../api";
import { Panel, StatTile, formatPct } from "./common";

export function CompressionView({ detail }: { detail: ChatResponse | null }) {
  const record = detail?.trace.find((r) => r.step === "compression");

  if (!detail || !record) {
    return (
      <Panel title="Compression (step 5)">
        <p className="text-sm text-gray-500">compression off / n/a</p>
      </Panel>
    );
  }

  const bypassed = record.applied === false || Boolean(record.bypass_reason);

  return (
    <Panel title="Compression (step 5)">
      {bypassed ? (
        <p className="text-sm text-gray-400">
          Bypassed — <span className="text-gray-200">{record.bypass_reason ?? "not applied"}</span>
        </p>
      ) : (
        <>
          <div className="flex items-baseline gap-3">
            <span className="text-lg text-gray-400">{record.tokens_before ?? "–"}</span>
            <span className="text-gray-600">→</span>
            <span className="text-lg text-gray-200">{record.tokens_after ?? "–"}</span>
            <span className="text-4xl font-bold text-cheap ml-auto">{formatPct(record.reduction_pct)}</span>
          </div>
          <p className="text-xs text-gray-500">reduction via {record.compressor ?? "unknown compressor"}</p>
        </>
      )}
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="CCR stored" value={record.ccr_stored ? "yes" : "no"} />
        <StatTile label="CCR retrievals" value={String(record.ccr_retrievals ?? 0)} />
        <StatTile label="→ cloud" value={String(record.ccr_retrievals_to_cloud ?? 0)} />
      </div>
    </Panel>
  );
}
