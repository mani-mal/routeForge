// Step 5 view — conditional context optimization (Headroom). Explains where
// "step 5" comes from (the 10-step flow), carries the on/off control for the
// step it describes, and shows tokens before/after, reduction %, which
// compressor fired, and CCR store/retrieve *counts* (never contents).
import type { ChatResponse } from "../api";
import { Panel, StatTile, formatPct } from "./common";

export function CompressionView({
  detail,
  headroomEnabled,
  onToggleHeadroom,
  toggling,
}: {
  detail: ChatResponse | null;
  headroomEnabled: boolean | null;
  onToggleHeadroom: () => void;
  toggling: boolean;
}) {
  const record = detail?.trace.find((r) => r.step === "compression");
  const bypassed = record ? record.applied === false || Boolean(record.bypass_reason) : false;
  const on = headroomEnabled === true;

  const toggle = (
    <button
      onClick={onToggleHeadroom}
      disabled={headroomEnabled === null || toggling}
      title="Headroom is step 5's compressor — toggle it, then send a request to see the effect"
      className={`flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium disabled:opacity-50 ${
        on ? "border-cheap bg-cheap/10 text-cheap" : "border-edge bg-ink/40 text-gray-400"
      }`}
    >
      <span className={`h-2 w-2 rounded-full ${on ? "bg-cheap" : "bg-gray-600"}`} />
      Headroom {on ? "on" : "off"}
    </button>
  );

  return (
    <Panel title="Compression" right={toggle}>
      <p className="text-xs text-gray-500 -mt-1">
        <span className="font-semibold text-gray-400">Step 5</span> of the 10-step flow — conditional
        context optimization. Compresses tool outputs &amp; history <em>after</em> the policy gate, so
        the router (step 6) prices fewer tokens.
      </p>

      {record && !bypassed ? (
        <>
          <div className="flex items-baseline gap-3">
            <span className="text-lg text-gray-400">{record.tokens_before ?? "–"}</span>
            <span className="text-gray-600">→</span>
            <span className="text-lg text-gray-200">{record.tokens_after ?? "–"}</span>
            <span className="text-4xl font-bold text-cheap ml-auto">{formatPct(record.reduction_pct)}</span>
          </div>
          <p className="text-xs text-gray-500">reduction via {record.compressor ?? "unknown compressor"}</p>
          <div className="grid grid-cols-3 gap-2">
            <StatTile label="CCR stored" value={record.ccr_stored ? "yes" : "no"} />
            <StatTile label="CCR retrievals" value={String(record.ccr_retrievals ?? 0)} />
            <StatTile label="→ cloud" value={String(record.ccr_retrievals_to_cloud ?? 0)} />
          </div>
        </>
      ) : record && bypassed ? (
        <p className="text-sm text-gray-400">
          Bypassed for this request — <span className="text-gray-200">{record.bypass_reason ?? "not applied"}</span>
        </p>
      ) : on ? (
        <p className="text-sm text-gray-500">
          {detail
            ? "No compression ran for this request (input below the minimum token threshold)."
            : "Select a request to see its step-5 compression."}
        </p>
      ) : (
        <p className="text-sm text-gray-500">
          Headroom is <span className="text-gray-300">off</span> — requests run uncompressed. Turn it on
          (top-right), then send a request to watch the token count drop.
        </p>
      )}
    </Panel>
  );
}
