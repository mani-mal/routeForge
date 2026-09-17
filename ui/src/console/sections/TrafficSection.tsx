// Traffic section — the per-application "answer desk" for the owner question
// "what's actually running, and how fast?" Tier mix, latency percentiles,
// and request volume.
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import type { RequestSummary } from "../../api";
import { TIER_COLOR } from "../../api";
import { Panel, StatTile, formatMs, formatPct } from "../../components/common";
import { useTheme } from "../../theme";
import { trafficAgg } from "../aggregate";

export function TrafficSection({ requests }: { requests: RequestSummary[] }) {
  const [theme] = useTheme();
  const dark = theme === "dark";
  const tooltipStyle = {
    background: dark ? "#111827" : "#ffffff",
    border: `1px solid ${dark ? "#1f2937" : "#e2e8f0"}`,
    color: dark ? "#e5e7eb" : "#111827",
    fontSize: 12,
  };

  const { volume, tierMix, p50, p95, multiStepPct } = trafficAgg(requests);

  const tierData = Object.entries(tierMix).map(([tier, count]) => ({
    name: tier,
    value: count,
    color: TIER_COLOR[tier] ?? TIER_COLOR["?"],
  }));

  return (
    <div className="flex flex-col gap-4">
      <Panel title="Traffic">
        <div>
          <div className="text-3xl font-bold text-gray-100">{volume.toLocaleString()}</div>
          <div className="text-xs text-gray-500">requests</div>
        </div>

        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Tier mix</h3>
          {tierData.length === 0 ? (
            <p className="text-sm text-gray-500">No requests yet.</p>
          ) : (
            <div className="h-40 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={tierData}
                    dataKey="value"
                    nameKey="name"
                    innerRadius={36}
                    outerRadius={60}
                    paddingAngle={2}
                  >
                    {tierData.map((d) => (
                      <Cell key={d.name} fill={d.color} />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={tooltipStyle} />
                </PieChart>
              </ResponsiveContainer>
            </div>
          )}
          <div className="flex flex-wrap gap-3 text-xs text-gray-400 mt-1">
            {tierData.map((d) => (
              <span key={d.name} className="flex items-center gap-1">
                <span className="h-2 w-2 rounded-full" style={{ backgroundColor: d.color }} />
                {d.name} {d.value}
              </span>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-3 gap-2">
          <StatTile label="p50 latency" value={formatMs(p50)} />
          <StatTile label="p95 latency" value={formatMs(p95)} />
          <StatTile label="% multi-step" value={formatPct(multiStepPct)} />
        </div>
      </Panel>
    </div>
  );
}
