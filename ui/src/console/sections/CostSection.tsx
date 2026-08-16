// Cost section — the per-application "answer desk" for the owner question
// "what did this app cost, and how much did we save?" Headline spend vs. the
// all-frontier baseline, the two savings levers kept visually separate
// (routing vs. compression — never merged into one number), and spend over
// time. Chart idioms lifted from components/CostDashboard.tsx.
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { RequestSummary } from "../../api";
import { Panel, StatTile, formatPct, formatUsd } from "../../components/common";
import { useTheme } from "../../theme";
import { costAgg, densityAgg } from "../aggregate";
import { LEVER_HEX, TIER_HEX } from "../../tokens";

// 2-3 significant figures — these are intelligence-density ratios (quality
// per 1k tokens / per $), not currency, so formatUsd/formatPct don't fit.
function formatDensity(v: number): string {
  if (!Number.isFinite(v) || v === 0) return "–";
  if (v >= 100) return v.toFixed(0);
  if (v >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

// Token-sourced, not hardcoded: routing is LEVER_HEX.routing (= TIER_HEX.mid
// blue), compression is LEVER_HEX.compression (= TIER_HEX.frontier amber).
// The old hardcoded #8b5cf6 sat one hex from the privacy signal #8B7BF7 (a
// savings lever vs a residency signal — unrelated meanings); routing blue
// keeps it unambiguously distinct from privacy violet on a projector.
const ROUTING_COLOR = LEVER_HEX.routing;
const COMPRESSION_COLOR = LEVER_HEX.compression;

export function CostSection({ requests }: { requests: RequestSummary[] }) {
  const [theme] = useTheme();
  const dark = theme === "dark";
  const gridStroke = dark ? "#1f2937" : "#e2e8f0";
  const baselineStroke = dark ? "#6b7280" : "#94a3b8";
  const tooltipStyle = {
    background: dark ? "#111827" : "#ffffff",
    border: `1px solid ${dark ? "#1f2937" : "#e2e8f0"}`,
    color: dark ? "#e5e7eb" : "#111827",
    fontSize: 12,
  };

  const { total, baseline, routingSaved, compressionSaved, savedPct } = costAgg(requests);
  const { tokenIntelligenceDensity, costIntelligenceDensity } = densityAgg(requests);
  const compressionRan = compressionSaved > 0;

  const savingsData = [{ name: "saved", routing: routingSaved, compression: compressionSaved }];

  const sorted = [...requests].sort((a, b) => a.created_seq - b.created_seq);
  let cumTotal = 0;
  let cumBaseline = 0;
  const lineData = sorted.map((r) => {
    cumTotal += r.total_usd;
    cumBaseline += r.baseline_usd;
    return { seq: r.created_seq, total: Number(cumTotal.toFixed(4)), baseline: Number(cumBaseline.toFixed(4)) };
  });

  return (
    <div className="flex flex-col gap-4">
      <Panel title="Cost">
        <div className="flex items-end justify-between gap-3">
          <div>
            <div className="text-3xl font-bold text-gray-100">{formatUsd(total)}</div>
            <div className="text-xs text-gray-500">actual spend vs. {formatUsd(baseline)} all-frontier baseline</div>
          </div>
          <div className="text-3xl font-bold text-cheap">{formatPct(savedPct)}</div>
        </div>

        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Savings by lever</h3>
          <div className="h-20 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={savingsData} layout="vertical" margin={{ top: 4, right: 8, bottom: 4, left: 8 }}>
                <XAxis type="number" hide />
                <YAxis type="category" dataKey="name" hide />
                <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => formatUsd(v)} />
                <Bar dataKey="routing" name="routing saved" fill={ROUTING_COLOR} radius={[3, 3, 3, 3]} barSize={16} />
                <Bar
                  dataKey="compression"
                  name="compression saved"
                  fill={COMPRESSION_COLOR}
                  radius={[3, 3, 3, 3]}
                  barSize={16}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="flex gap-4 text-xs text-gray-400 mt-1">
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: ROUTING_COLOR }} /> routing{" "}
              {formatUsd(routingSaved)}
            </span>
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: COMPRESSION_COLOR }} /> compression{" "}
              {compressionRan ? formatUsd(compressionSaved) : "off / not run"}
            </span>
          </div>
        </div>

        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Cost over time</h3>
          <div className="h-40 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={lineData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke={gridStroke} strokeDasharray="3 3" />
                <XAxis dataKey="seq" hide />
                <YAxis hide />
                <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => formatUsd(v)} />
                <Line
                  type="monotone"
                  dataKey="baseline"
                  stroke={baselineStroke}
                  dot={false}
                  strokeWidth={1.5}
                  name="baseline"
                />
                <Line
                  type="monotone"
                  dataKey="total"
                  stroke={TIER_HEX.cheap}
                  dot={false}
                  strokeWidth={2}
                  name="actual"
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </Panel>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatTile label="Total spend" value={formatUsd(total)} />
        <StatTile label="All-frontier baseline" value={formatUsd(baseline)} />
        <StatTile label="Routing saved" value={formatUsd(routingSaved)} />
        <StatTile label="Compression saved" value={compressionRan ? formatUsd(compressionSaved) : "off / not run"} />
        <StatTile
          label="Token Intelligence Density"
          value={formatDensity(tokenIntelligenceDensity)}
          info="quality delivered per 1k tokens — needs a per-model quality score"
        />
        <StatTile
          label="Cost Intelligence Density"
          value={formatDensity(costIntelligenceDensity)}
          info="quality delivered per $ — needs a per-model quality score"
        />
      </div>
    </div>
  );
}
