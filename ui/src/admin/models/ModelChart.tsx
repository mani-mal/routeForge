import { CartesianGrid, Legend, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from "recharts";
import type { ModelMetricsRow } from "../../api";
import { SIGNAL_HEX, TIER_HEX } from "../../tokens";
import { money, percent } from "./format";

interface Pt {
  model: string;
  placement: string;
  x: number;
  y: number;
  spend: number;
}

const PLACEMENT_COLOR: Record<string, string> = { onprem: SIGNAL_HEX.privacy, cloud: TIER_HEX.frontier };
const PLACEMENT_NAME: Record<string, string> = { onprem: "On-prem (circle)", cloud: "Cloud (diamond)" };

function Dot(props: { cx?: number; cy?: number; fill?: string; payload?: Pt; size?: number }) {
  const { cx = 0, cy = 0, fill, payload } = props;
  const r = Math.max(5, Math.min(18, Math.sqrt(props.size ?? 80) / 2));
  if (payload?.placement === "cloud") {
    return <polygon points={`${cx},${cy - r} ${cx + r},${cy} ${cx},${cy + r} ${cx - r},${cy}`} fill={fill} fillOpacity={0.8} />;
  }
  return <circle cx={cx} cy={cy} r={r} fill={fill} fillOpacity={0.8} />;
}

export function ModelChart({ rows }: { rows: ModelMetricsRow[] }) {
  // x: cost per successful task, falling back to cost per task when nothing is
  // labelled; y: reported success is not available per model, so completed.
  const useSuccessCost = rows.some((r) => r.cost_per_successful_task.value !== null);
  const pts: Pt[] = [];
  let omitted = 0;
  for (const r of rows) {
    const x = useSuccessCost ? r.cost_per_successful_task.value : r.cost_per_task.attempted;
    const y = r.completed.value;
    if (x === null || y === null || x === undefined) {
      omitted += 1;
      continue;
    }
    pts.push({ model: r.model, placement: r.placement, x, y, spend: r.spend });
  }
  const placements = Array.from(new Set(pts.map((p) => p.placement)));

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-gray-300">
        X: {useSuccessCost ? "cost per successful task" : "cost per request (no success reports yet)"} · Y: completed
        without failure (not a quality measure) · size: spend
      </p>
      {pts.length === 0 ? (
        <p className="text-sm text-gray-300">Not enough data to chart yet.</p>
      ) : (
        <div style={{ width: "100%", height: 300 }}>
          <ResponsiveContainer>
            <ScatterChart margin={{ top: 10, right: 20, bottom: 20, left: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a3140" />
              <XAxis
                type="number"
                dataKey="x"
                name="Cost"
                tickFormatter={(v: number) => money(v)}
                stroke="#9ca3af"
                label={{ value: useSuccessCost ? "Cost per successful task" : "Cost per request", position: "bottom", fill: "#d1d5db", fontSize: 12 }}
              />
              <YAxis
                type="number"
                dataKey="y"
                name="Completed"
                domain={[0, 1]}
                tickFormatter={(v: number) => percent(v, 0)}
                stroke="#9ca3af"
              />
              <ZAxis type="number" dataKey="spend" range={[80, 900]} name="Spend" />
              <Tooltip
                cursor={{ strokeDasharray: "3 3" }}
                content={({ payload }) => {
                  const p = payload?.[0]?.payload as Pt | undefined;
                  if (!p) return null;
                  return (
                    <div className="rounded-md border border-edge bg-panel px-3 py-2 text-xs text-gray-100">
                      <div className="font-semibold">{p.model}</div>
                      <div>{PLACEMENT_NAME[p.placement] ?? p.placement}</div>
                      <div>cost: {money(p.x)}</div>
                      <div>completed: {percent(p.y)}</div>
                      <div>spend: {money(p.spend)}</div>
                    </div>
                  );
                }}
              />
              <Legend />
              {placements.map((pl) => (
                <Scatter
                  key={pl}
                  name={PLACEMENT_NAME[pl] ?? pl}
                  data={pts.filter((p) => p.placement === pl)}
                  fill={PLACEMENT_COLOR[pl] ?? "#9ca3af"}
                  shape={(props: unknown) => <Dot {...(props as object)} />}
                />
              ))}
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      )}
      {omitted > 0 && (
        <p className="text-xs text-gray-300">
          {omitted} model{omitted === 1 ? "" : "s"} omitted: insufficient data for one of the axes.
        </p>
      )}
    </div>
  );
}
