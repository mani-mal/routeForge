// Overview section — the answer desk at a glance. One headline number
// (percent saved vs. all-frontier) plus a handful of stat tiles, one pulled
// from each of the three aggregators, so an owner gets the whole story
// without opening a tab.
import type { RequestSummary } from "../../api";
import { Panel, StatTile, formatPct, formatUsd } from "../../components/common";
import { costAgg, privacyAgg, trafficAgg } from "../aggregate";

export function OverviewSection({ requests }: { requests: RequestSummary[] }) {
  const cost = costAgg(requests);
  const privacy = privacyAgg(requests);
  const traffic = trafficAgg(requests);
  const totalSaved = cost.baseline - cost.total;

  return (
    <div className="flex flex-col gap-4">
      <Panel title="Overview">
        <div className="flex items-end justify-between gap-3">
          <div>
            <div className="text-xs text-gray-500">saved vs. all-frontier baseline</div>
            <div className="text-4xl font-bold text-cheap">{formatPct(cost.savedPct)}</div>
          </div>
          <div className="text-right">
            <div className="text-xs text-gray-500">requests</div>
            <div className="text-xl font-semibold text-gray-200">{traffic.volume.toLocaleString()}</div>
          </div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatTile label="Total spend" value={formatUsd(cost.total)} />
          <StatTile label="Total saved" value={formatUsd(totalSaved)} />
          <StatTile label="Requests with PII" value={privacy.pii.toLocaleString()} />
          <StatTile label="Kept off cloud" value={privacy.keptOffCloud.toLocaleString()} />
        </div>
      </Panel>
    </div>
  );
}
