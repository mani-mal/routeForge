import type { ModelMetricsRow } from "../../api";
import { percent } from "./format";

export function Diagnostics({
  routeReasons,
  rows,
}: {
  routeReasons: Record<string, number>;
  rows: ModelMetricsRow[];
}) {
  const reasons = Object.entries(routeReasons).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...reasons.map(([, n]) => n));
  return (
    <details className="bg-panel border border-edge rounded-lg p-4">
      <summary className="cursor-pointer select-none text-xs font-display uppercase tracking-[0.12em] text-gray-300">
        Diagnostics
      </summary>
      <div className="mt-3 grid gap-6 lg:grid-cols-2">
        <section>
          <h3 className="mb-2 text-sm font-semibold text-gray-100">Routing reasons</h3>
          {reasons.length === 0 ? (
            <p className="text-sm text-gray-300">No routing reasons recorded in this window.</p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {reasons.map(([k, n]) => (
                <li key={k} className="grid grid-cols-[minmax(120px,1fr)_2fr_auto] items-center gap-2 text-sm">
                  <span className="truncate text-gray-100">{k.replace(/_/g, " ")}</span>
                  <span className="h-2 rounded bg-ink/60" aria-hidden="true">
                    <span className="block h-2 rounded bg-accent" style={{ width: `${(n / max) * 100}%` }} />
                  </span>
                  <span className="font-mono text-gray-100">{n.toLocaleString()}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section>
          <h3 className="mb-2 text-sm font-semibold text-gray-100">Cache hit and failovers per model</h3>
          {rows.length === 0 ? (
            <p className="text-sm text-gray-300">No model traffic in this window.</p>
          ) : (
            <table className="w-full text-sm text-gray-100">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-[0.12em] text-gray-300">
                  <th className="py-1 font-semibold">Model</th>
                  <th className="py-1 font-semibold">Cache hit</th>
                  <th className="py-1 font-semibold">Failovers</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.model} className="border-t border-edge">
                    <td className="py-1 pr-2 truncate">{r.model}</td>
                    <td className="py-1 font-mono">{percent(r.cache_hit_rate)}</td>
                    <td className="py-1 font-mono">{r.failovers.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </details>
  );
}
