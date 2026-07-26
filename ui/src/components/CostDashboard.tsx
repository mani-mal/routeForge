// Panel 3 — cost dashboard. Headline spend vs. all-frontier baseline, the
// two independent savings levers (routing vs. compression, never merged),
// tier distribution, cost-over-time, and latency/on-prem tiles.
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { RequestSummary, Stats } from "../api";
import { TIER_COLOR } from "../api";
import { useTheme } from "../theme";
import { Panel, StatTile, formatMs, formatPct, formatUsd } from "./common";

const ROUTING_COLOR = "#38bdf8";
const COMPRESSION_COLOR = "#f59e0b";

export function CostDashboard({ stats, requests }: { stats: Stats | null; requests: RequestSummary[] }) {
  // Recharts needs concrete colours, so chart chrome follows the active theme.
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
  const totalUsd = stats?.total_usd ?? 0;
  const baselineUsd = stats?.baseline_usd ?? 0;
  const pctSaved = baselineUsd > 0 ? ((baselineUsd - totalUsd) / baselineUsd) * 100 : 0;

  const savingsData = [
    {
      name: "saved",
      routing: stats?.routing_saved_usd ?? 0,
      compression: stats?.compression_saved_usd ?? 0,
    },
  ];

  const tierData = Object.entries(stats?.tier_distribution ?? {}).map(([tier, count]) => ({
    name: tier,
    value: count,
    color: TIER_COLOR[tier] ?? TIER_COLOR["?"],
  }));

  const sorted = [...requests].sort((a, b) => a.created_seq - b.created_seq);
  let cumTotal = 0;
  let cumBaseline = 0;
  const lineData = sorted.map((r) => {
    cumTotal += r.total_usd;
    cumBaseline += r.baseline_usd;
    return { seq: r.created_seq, total: Number(cumTotal.toFixed(4)), baseline: Number(cumBaseline.toFixed(4)) };
  });

  return (
    <Panel title="Cost dashboard" className="h-full">
      <div className="flex items-end justify-between gap-3">
        <div>
          <div className="text-3xl font-bold text-gray-100">{formatUsd(totalUsd)}</div>
          <div className="text-xs text-gray-500">actual spend vs. {formatUsd(baselineUsd)} all-frontier baseline</div>
        </div>
        <div className="text-3xl font-bold text-cheap">{formatPct(pctSaved)}</div>
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
              <Bar dataKey="compression" name="compression saved" fill={COMPRESSION_COLOR} radius={[3, 3, 3, 3]} barSize={16} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="flex gap-4 text-xs text-gray-400 mt-1">
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: ROUTING_COLOR }} /> routing{" "}
            {formatUsd(stats?.routing_saved_usd)}
          </span>
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: COMPRESSION_COLOR }} /> compression{" "}
            {formatUsd(stats?.compression_saved_usd)}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Tier distribution</h3>
          <div className="h-32 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={tierData} dataKey="value" nameKey="name" innerRadius={28} outerRadius={48} paddingAngle={2}>
                  {tierData.map((d) => (
                    <Cell key={d.name} fill={d.color} />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">Cost over time</h3>
          <div className="h-32 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={lineData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke={gridStroke} strokeDasharray="3 3" />
                <XAxis dataKey="seq" hide />
                <YAxis hide />
                <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => formatUsd(v)} />
                <Line type="monotone" dataKey="baseline" stroke={baselineStroke} dot={false} strokeWidth={1.5} name="baseline" />
                <Line type="monotone" dataKey="total" stroke="#22c55e" dot={false} strokeWidth={2} name="actual" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <StatTile label="p50 latency" value={formatMs(stats?.latency_p50_ms)} />
        <StatTile label="p95 latency" value={formatMs(stats?.latency_p95_ms)} />
        <StatTile label="on-prem kept" value={String(stats?.onprem_count ?? 0)} />
      </div>
    </Panel>
  );
}
