import { CartesianGrid, Legend, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from "recharts";
import type { ChartPoint, ModelMetricsRow } from "../../api";
import { SIGNAL_HEX, TIER_HEX } from "../../tokens";
import { money, percent } from "./format";

interface Pt extends ChartPoint {
  model: string;
  placement: string;
  spend: number;
}

const PLACEMENT_COLOR: Record<string, string> = { onprem: SIGNAL_HEX.privacy, cloud: TIER_HEX.frontier };
const PLACEMENT_NAME: Record<string, string> = {
  onprem: "On-prem (circle)",
  cloud: "Cloud (diamond)",
  unknown: "Placement unknown (circle)",
};
const X_NAME = { per_successful_task: "cost per successful task", per_task: "cost per request" } as const;
const Y_NAME = { reported: "reported success", completed: "completed without failure" } as const;

function Dot(props: { cx?: number; cy?: number; fill?: string; payload?: Pt; size?: number }) {
  const { cx = 0, cy = 0, fill, payload } = props;
  const r = Math.max(5, Math.min(18, Math.sqrt(props.size ?? 80) / 2));
  if (payload?.placement === "cloud") {
    return <polygon points={`${cx},${cy - r} ${cx + r},${cy} ${cx},${cy + r} ${cx - r},${cy}`} fill={fill} fillOpacity={0.8} />;
  }
  return <circle cx={cx} cy={cy} r={r} fill={fill} fillOpacity={0.8} />;
}

/** Name an axis from the kinds the server chose; "mixed" when models differ. */
function axisName<K extends string>(kinds: K[], names: Record<K, string>, mixed: string): string {
  const set = Array.from(new Set(kinds));
  return set.length === 1 ? names[set[0]] : mixed;
}

export type ChartView = "workload" | "aa";

interface AaPt {
  model: string;
  placement: string;
  x: number;
  y: number;
}

function AaChart({ rows }: { rows: ModelMetricsRow[] }) {
  const pts: AaPt[] = [];
  let omitted = 0;
  for (const r of rows) {
    const p = r.published;
    if (p && p.price_blended != null && p.intelligence_index != null) {
      pts.push({ model: r.model, placement: r.placement, x: p.price_blended, y: p.intelligence_index });
    } else omitted += 1;
  }
  const placements = Array.from(new Set(pts.map((p) => p.placement)));
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-gray-300">
        X: blended price, USD per 1M tokens (Artificial Analysis list price) · Y: Artificial Analysis Intelligence Index.
        Published figures, not measured here.
      </p>
      {pts.length === 0 ? (
        <p className="text-sm text-gray-300">No mapped model has both a blended price and an intelligence index.</p>
      ) : (
        <div style={{ width: "100%", height: 300 }}>
          <ResponsiveContainer>
            <ScatterChart margin={{ top: 10, right: 20, bottom: 20, left: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a3140" />
              <XAxis type="number" dataKey="x" name="Blended price" tickFormatter={(v: number) => money(v)} stroke="#9ca3af" label={{ value: "blended price (USD per 1M tokens)", position: "bottom", fill: "#d1d5db", fontSize: 12 }} />
              <YAxis type="number" dataKey="y" name="Intelligence" stroke="#9ca3af" domain={["auto", "auto"]} />
              <ZAxis range={[120, 120]} />
              <Tooltip
                cursor={{ strokeDasharray: "3 3" }}
                content={({ payload }) => {
                  const p = payload?.[0]?.payload as AaPt | undefined;
                  if (!p) return null;
                  return (
                    <div className="rounded-md border border-edge bg-panel px-3 py-2 text-xs text-gray-100">
                      <div className="font-semibold">{p.model}</div>
                      <div>{PLACEMENT_NAME[p.placement] ?? p.placement}</div>
                      <div>blended price: {money(p.x)} per 1M tokens</div>
                      <div>intelligence index: {p.y.toFixed(1)}</div>
                    </div>
                  );
                }}
              />
              <Legend />
              {placements.map((pl) => (
                <Scatter key={pl} name={PLACEMENT_NAME[pl] ?? pl} data={pts.filter((p) => p.placement === pl)} fill={PLACEMENT_COLOR[pl] ?? "#9ca3af"} shape={(props: unknown) => <Dot {...(props as object)} />} />
              ))}
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      )}
      {omitted > 0 && (
        <p className="text-xs text-gray-300">
          {omitted} model{omitted === 1 ? "" : "s"} omitted: not mapped, or Artificial Analysis publishes no blended price
          or intelligence index for {omitted === 1 ? "it" : "them"}.
        </p>
      )}
    </div>
  );
}

export function ModelChart({ rows, view, onView }: { rows: ModelMetricsRow[]; view: ChartView; onView: (v: ChartView) => void }) {
  // The server decides each model's point (and its kinds); this only plots it.
  const pts: Pt[] = [];
  let omitted = 0;
  for (const r of rows) {
    if (!r.chart) {
      omitted += 1;
      continue;
    }
    pts.push({ ...r.chart, model: r.model, placement: r.placement, spend: r.spend });
  }
  const placements = Array.from(new Set(pts.map((p) => p.placement)));
  const xName = axisName(pts.map((p) => p.x_kind), X_NAME, "cost per successful task or per request (see tooltip)");
  const yName = axisName(pts.map((p) => p.y_kind), Y_NAME, "reported success or completed (see tooltip)");

  const toggle = (
    <div role="group" aria-label="Chart view" className="flex gap-2">
      {([["workload", "Your workload"], ["aa", "Published AA comparison"]] as const).map(([v, label]) => (
        <button
          key={v}
          type="button"
          aria-pressed={view === v}
          onClick={() => onView(v)}
          className={`rounded-md border px-3 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${view === v ? "border-accent text-gray-50" : "border-edge text-gray-300 hover:border-accent"}`}
        >
          {view === v && <span aria-hidden="true">● </span>}
          {label}
        </button>
      ))}
    </div>
  );
  if (view === "aa")
    return (
      <div className="flex flex-col gap-2">
        {toggle}
        <AaChart rows={rows} />
      </div>
    );

  return (
    <div className="flex flex-col gap-2">
      {toggle}
      <p className="text-xs text-gray-300">
        X: {xName} · Y: {yName}
        {pts.some((p) => p.y_kind === "completed") ? " (completed without failure is not a quality measure)" : ""} ·
        size: spend
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
                label={{ value: xName, position: "bottom", fill: "#d1d5db", fontSize: 12 }}
              />
              <YAxis type="number" dataKey="y" name="Success" domain={[0, 1]} tickFormatter={(v: number) => percent(v, 0)} stroke="#9ca3af" />
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
                      <div>
                        {X_NAME[p.x_kind]}: {money(p.x)}
                      </div>
                      <div>
                        {Y_NAME[p.y_kind]}: {percent(p.y)}
                      </div>
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
          {omitted} model{omitted === 1 ? "" : "s"} omitted: not enough data for a point (needs a cost figure and at
          least 30 requests or 20 labelled tasks).
        </p>
      )}
    </div>
  );
}
