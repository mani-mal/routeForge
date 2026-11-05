import { useCallback, useState } from "react";
import type { ReactNode } from "react";
import type { ModelMetricsRow } from "../../api";
import { ModelSidePanel } from "./ModelSidePanel";
import { comparabilityLabel, field, insufficientText, money, ms, percent, pub, pubMoney, statusLabel, tps } from "./format";

interface Col {
  key: string;
  label: string;
  tip: string;
  optional?: boolean;
  /** Published (Artificial Analysis) group; the first one carries the group border. */
  published?: boolean;
  first?: boolean;
  sort: (r: ModelMetricsRow) => number | string | null;
  render: (r: ModelMetricsRow) => ReactNode;
}

const ALL_APPS = " Model-level over all applications: ignores the application and workload filters.";

const COLS: Col[] = [
  {
    key: "model",
    label: "Model",
    tip: "Model name and where it runs (on-prem or cloud).",
    sort: (r) => r.model,
    render: (r) => (
      <span className="min-w-0">
        <span className="block truncate font-medium text-gray-50">{r.model}</span>
        <span className="block text-xs text-gray-300">{r.placement === "onprem" ? "on-prem" : r.placement}</span>
      </span>
    ),
  },
  {
    key: "status",
    label: "Status",
    tip: "Sorted by severity, worst first: cost anomaly, performance degradation, cache regression, price drift, insufficient data, healthy. Hover a status for the reasons.",
    sort: (r) => -r.status_rank, // server-ranked; descending puts the worst first
    render: (r) => {
      const s = statusLabel(r.status);
      return (
        <span title={r.status_reasons.join("; ")} className="text-gray-100">
          <span aria-hidden="true">{s.icon} </span>
          {s.label}
        </span>
      );
    },
  },
  {
    key: "spend",
    label: "Spend",
    tip: "Total cost of this model's requests in the window.",
    sort: (r) => r.spend,
    render: (r) => <span className="font-mono">{money(r.spend)}</span>,
  },
  {
    key: "completed",
    label: "Completed",
    tip: "Share of requests that completed without failure. Not a quality measure. Minimum 30 requests.",
    sort: (r) => r.completed.value,
    render: (r) => (
      <span className={r.completed.value === null ? "text-xs text-gray-300" : "font-mono"}>
        {field(r.completed, (v) => percent(v))}
      </span>
    ),
  },
  {
    key: "reported",
    label: "Successful (reported)",
    tip: "Share of this model's labelled tasks reported successful (a task is credited to the model that carried most of its cost). Minimum 20 labelled tasks.",
    sort: (r) => r.successful_reported.value,
    render: (r) => (
      <span className={r.successful_reported.value === null ? "text-xs text-gray-300" : "font-mono"}>
        {field(r.successful_reported, (v) => percent(v))}
      </span>
    ),
  },
  {
    key: "cost",
    label: "Cost / successful task",
    tip: "Cost of labelled tasks divided by successful labelled tasks, with the number of labelled tasks (n).",
    sort: (r) => r.cost_per_successful_task.value,
    render: (r) => (
      <span className={r.cost_per_successful_task.value === null ? "text-xs text-gray-300" : "font-mono"}>
        {r.cost_per_successful_task.value === null
          ? insufficientText(r.cost_per_successful_task.n)
          : `${money(r.cost_per_successful_task.value)} (n=${r.cost_per_successful_task.n})`}
      </span>
    ),
  },
  {
    key: "speed",
    label: "Speed (all apps)",
    tip: "Output tokens per second on non-streamed ok attempts; minimum 30." + ALL_APPS,
    sort: (r) => r.speed_tps.value,
    render: (r) => (
      <span className={r.speed_tps.value === null ? "text-xs text-gray-300" : "font-mono"}>
        {field(r.speed_tps, tps)}
      </span>
    ),
  },
  {
    key: "p95",
    label: "p95 time",
    tip: "95th percentile request latency in the window.",
    sort: (r) => r.p95_latency_ms,
    render: (r) => <span className="font-mono">{ms(r.p95_latency_ms)}</span>,
  },
  {
    key: "ttft50",
    label: "TTFT p50 (all apps)",
    optional: true,
    tip: "Median time to first token on streamed attempts; minimum 10." + ALL_APPS,
    sort: (r) => r.ttft_ms.p50,
    render: (r) => (
      <span className={r.ttft_ms.p50 === null ? "text-xs text-gray-300" : "font-mono"}>
        {r.ttft_ms.p50 === null ? insufficientText(r.ttft_ms.n) : ms(r.ttft_ms.p50)}
      </span>
    ),
  },
  {
    key: "ttft95",
    label: "TTFT p95 (all apps)",
    optional: true,
    tip: "95th percentile time to first token on streamed attempts; minimum 10." + ALL_APPS,
    sort: (r) => r.ttft_ms.p95,
    render: (r) => (
      <span className={r.ttft_ms.p95 === null ? "text-xs text-gray-300" : "font-mono"}>
        {r.ttft_ms.p95 === null ? insufficientText(r.ttft_ms.n) : ms(r.ttft_ms.p95)}
      </span>
    ),
  },
  {
    key: "errors",
    label: "Errors",
    optional: true,
    tip: "Requests that failed (backend failure, verification failure or context overflow), attributed to the last model tried.",
    sort: (r) => r.errors,
    render: (r) => <span className="font-mono">{r.errors.toLocaleString()}</span>,
  },
  {
    key: "failovers",
    label: "Failovers",
    optional: true,
    tip: "Requests that moved to another endpoint after a failure.",
    sort: (r) => r.failovers,
    render: (r) => <span className="font-mono">{r.failovers.toLocaleString()}</span>,
  },
  {
    key: "cache",
    label: "Cache hit",
    optional: true,
    tip: "Share of prompt tokens served from the provider's prompt cache.",
    sort: (r) => r.cache_hit_rate,
    render: (r) => <span className="font-mono">{percent(r.cache_hit_rate)}</span>,
  },
  {
    key: "aa_intel",
    label: "Intelligence",
    published: true,
    first: true,
    tip: "Artificial Analysis Intelligence Index. Published by Artificial Analysis, not measured here. — means not published or not mapped.",
    sort: (r) => r.published?.intelligence_index ?? null,
    render: (r) => <span className="font-mono">{pub(r.published?.intelligence_index)}</span>,
  },
  {
    key: "aa_speed",
    label: "Speed (AA)",
    published: true,
    tip: "Output tokens per second as published by Artificial Analysis for its own chosen provider, on ~1k-token prompts.",
    sort: (r) => r.published?.output_tps ?? null,
    render: (r) => (
      <span className="font-mono">{r.published?.output_tps == null ? "—" : `${r.published.output_tps.toFixed(1)} tok/s`}</span>
    ),
  },
  {
    key: "aa_price",
    label: "Price in / out (AA)",
    published: true,
    tip: "Artificial Analysis list price, USD per 1M tokens, input / output.",
    sort: (r) => r.published?.price_in ?? null,
    render: (r) => (
      <span className="font-mono">
        {r.published ? `${pubMoney(r.published.price_in)} / ${pubMoney(r.published.price_out)}` : "—"}
      </span>
    ),
  },
  {
    key: "aa_comparable",
    label: "Comparable?",
    published: true,
    tip: "Whether the published figures can be compared with what RouteForge measured for this model. Decided by the server; hover a badge for the reasons.",
    sort: (r) => r.comparability?.overall_rank ?? null,
    render: (r) => {
      if (!r.comparability) return <span className="font-mono">—</span>;
      const c = comparabilityLabel(r.comparability.overall);
      return (
        <span title={r.comparability.reasons.join("; ")} className="text-gray-100">
          <span aria-hidden="true">{c.icon} </span>
          {c.label}
        </span>
      );
    },
  },
];


function cmp(a: number | string | null, b: number | string | null): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1; // empties last
  if (b === null) return -1;
  return typeof a === "string" || typeof b === "string" ? String(a).localeCompare(String(b)) : a - b;
}

export function ModelTable({ rows }: { rows: ModelMetricsRow[] }) {
  const [extra, setExtra] = useState<Set<string>>(new Set());
  const [sortKey, setSortKey] = useState("spend");
  const [desc, setDesc] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);

  const cols = COLS.filter((c) => !c.optional || extra.has(c.key));
  const sortCol = COLS.find((c) => c.key === sortKey) ?? COLS[0];
  const sorted = [...rows].sort((a, b) => {
    const r = cmp(sortCol.sort(a), sortCol.sort(b));
    // keep nulls last regardless of direction
    const an = sortCol.sort(a) === null;
    const bn = sortCol.sort(b) === null;
    if (an !== bn) return r;
    return desc ? -r : r;
  });
  const measuredN = cols.filter((c) => !c.published).length;
  const publishedN = cols.length - measuredN;
  const grid = { gridTemplateColumns: `minmax(150px,2fr) repeat(${cols.length - 1}, minmax(110px,1fr))` };
  const close = useCallback(() => setSelected(null), []);
  const selectedRow = rows.find((r) => r.model === selected) ?? null;

  function toggleSort(k: string) {
    if (k === sortKey) setDesc(!desc);
    else {
      setSortKey(k);
      setDesc(true);
    }
  }

  function toggleCol(k: string) {
    const n = new Set(extra);
    if (n.has(k)) n.delete(k);
    else n.add(k);
    setExtra(n);
  }

  if (rows.length === 0) return <p className="text-sm text-gray-300">No model traffic in this window.</p>;

  return (
    <div className="flex flex-col gap-2">
      <details className="text-sm text-gray-200">
        <summary className="cursor-pointer select-none text-xs font-semibold uppercase tracking-[0.12em] text-gray-300">
          Columns
        </summary>
        <div className="mt-2 flex flex-wrap gap-4">
          {COLS.filter((c) => c.optional).map((c) => (
            <label key={c.key} className="flex items-center gap-1.5" title={c.tip}>
              <input type="checkbox" checked={extra.has(c.key)} onChange={() => toggleCol(c.key)} />
              {c.label}
            </label>
          ))}
        </div>
      </details>

      <div className="overflow-x-auto">
        <div className="min-w-max flex flex-col gap-1.5">
          <div className="grid gap-3 px-3" style={grid} aria-hidden="true">
            <span
              className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-400"
              style={{ gridColumn: `span ${measuredN}` }}
            >
              Measured here
            </span>
            <span
              className="border-l border-edge pl-3 text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-400"
              style={{ gridColumn: `span ${publishedN}` }}
            >
              Published · Artificial Analysis
            </span>
          </div>
          <div className="grid gap-3 px-3" style={grid} role="row">
            {cols.map((c) => (
              <button
                key={c.key}
                type="button"
                onClick={() => toggleSort(c.key)}
                title={c.tip}
                aria-sort={sortKey === c.key ? (desc ? "descending" : "ascending") : "none"}
                className={`text-left text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-300 hover:text-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${c.first ? "border-l border-edge pl-3" : ""}`}
              >
                {c.label}
                {sortKey === c.key && <span aria-hidden="true">{desc ? " ▼" : " ▲"}</span>}
              </button>
            ))}
          </div>
          {sorted.map((r) => (
            <button
              key={r.model}
              type="button"
              onClick={() => setSelected(r.model)}
              aria-label={`Show details for ${r.model}`}
              className="grid gap-3 w-full items-center rounded-md border border-edge bg-ink/40 px-3 py-2 text-left text-sm text-gray-100 hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              style={grid}
            >
              {cols.map((c) => (
                <span key={c.key} className={`min-w-0 ${c.first ? "border-l border-edge pl-3" : ""}`}>
                  {c.render(r)}
                </span>
              ))}
            </button>
          ))}
        </div>
      </div>
      {selectedRow && <ModelSidePanel row={selectedRow} onClose={close} />}
    </div>
  );
}
