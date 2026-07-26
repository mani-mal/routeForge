// Privacy section — the per-application "answer desk" for the owner question
// "did anything sensitive leak?" Entity chips are TYPES only, never raw
// values — the honesty rule carried through the whole console.
import type { RequestSummary } from "../../api";
import { Panel, StatTile } from "../../components/common";
import { privacyAgg } from "../aggregate";

export function PrivacySection({ requests }: { requests: RequestSummary[] }) {
  const { pii, onprem, keptOffCloud, entityTypes } = privacyAgg(requests);

  return (
    <div className="flex flex-col gap-4">
      <Panel title="Privacy">
        <p className="text-sm text-gray-400">Sensitive requests kept on your infrastructure.</p>

        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <StatTile label="Requests with PII" value={pii.toLocaleString()} />
          <StatTile label="Forced on-prem" value={onprem.toLocaleString()} />
          <StatTile label="Kept off cloud" value={keptOffCloud.toLocaleString()} />
        </div>

        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Detected entity types</h3>
          {entityTypes.length === 0 ? (
            <p className="text-sm text-gray-500">None detected.</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {entityTypes.map((t) => (
                <span key={t} className="rounded bg-frontier/20 text-frontier px-1.5 py-0.5 text-xs font-semibold">
                  {t}
                </span>
              ))}
            </div>
          )}
        </div>
      </Panel>
    </div>
  );
}
