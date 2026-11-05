import { useEffect } from "react";
import type { ReactNode } from "react";
import type { ModelMetricsRow } from "../../api";
import { comparabilityLabel, field, insufficientText, money, ms, percent, pub, pubMoney, secs, stamp, statusLabel, tps } from "./format";

function Item({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 border-b border-edge pb-2">
      <dt className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-400">{label}</dt>
      <dd className="text-sm text-gray-50">{children}</dd>
    </div>
  );
}

function Published({ row }: { row: ModelMetricsRow }) {
  const p = row.published;
  const cmp = row.comparability;
  const per: [string, string][] = cmp
    ? [
        ["Speed", cmp.speed],
        ["TTFT", cmp.ttft],
        ["Price", cmp.price],
      ]
    : [];
  return (
    <section className="mt-4 border-t border-edge pt-3" aria-label="Published data">
      <h3 className="mb-2 font-display text-sm font-semibold text-gray-50">Published · Artificial Analysis</h3>
      {!p ? (
        <p className="text-sm text-gray-300">
          No published figures for this model.
          {cmp && cmp.reasons.length > 0 ? ` ${cmp.reasons[0]}.` : ""}
        </p>
      ) : (
        <dl className="flex flex-col gap-2">
          <Item label="AA model">
            {p.name ?? p.slug}
            <span className="block text-xs text-gray-300">
              slug {p.slug}
              {p.creator ? ` · ${p.creator}` : ""}
            </span>
          </Item>
          <Item label="Intelligence / coding / math index">
            {pub(p.intelligence_index)} / {pub(p.coding_index)} / {pub(p.math_index)}
          </Item>
          <Item label="Speed (AA)">{p.output_tps == null ? "—" : `${p.output_tps.toFixed(1)} tok/s`}</Item>
          <Item label="TTFT (AA, seconds)">{secs(p.ttft_s)}</Item>
          <Item label="Time to first answer token (AA, seconds)">{secs(p.ttfat_s)}</Item>
          <Item label="Price in / out / blended (USD per 1M tokens)">
            {pubMoney(p.price_in)} / {pubMoney(p.price_out)} / {pubMoney(p.price_blended)}
          </Item>
          <Item label="Snapshot">
            #{p.snapshot_id} · {stamp(p.retrieved_at)}
            {p.dated_at_upload ? " (date of upload, not of fetch)" : ""} · {p.index_version ? `index ${p.index_version}` : "index version not recorded"}
          </Item>
        </dl>
      )}
      {cmp && (
        <div className="mt-3">
          <div className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-400">
            Comparability with your measurements
          </div>
          <p className="mt-1 text-sm text-gray-50">
            <span aria-hidden="true">{comparabilityLabel(cmp.overall).icon} </span>
            Overall: {comparabilityLabel(cmp.overall).label}
          </p>
          <ul className="mt-1 text-sm text-gray-200">
            {per.map(([k, v]) => (
              <li key={k}>
                <span aria-hidden="true">{comparabilityLabel(v).icon} </span>
                {k}: {comparabilityLabel(v).label}
              </li>
            ))}
          </ul>
          {cmp.reasons.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-sm text-gray-200">
              {cmp.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

export function ModelSidePanel({ row, onClose }: { row: ModelMetricsRow; onClose: () => void }) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const s = statusLabel(row.status);
  const c = row.completed;
  const cpt = row.cost_per_task;
  return (
    <aside
      role="dialog"
      aria-label={`Details for ${row.model}`}
      className="fixed inset-y-0 right-0 z-50 w-full max-w-md overflow-y-auto border-l border-edge bg-panel p-4 shadow-xl"
    >
      <div className="mb-3 flex items-start justify-between gap-2">
        <h2 className="font-display text-base font-semibold text-gray-50 break-all">{row.model}</h2>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-edge px-2 py-1 text-sm text-gray-100 hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Close (Esc)
        </button>
      </div>
      <dl className="flex flex-col gap-2">
        <Item label="Placement">{row.placement === "onprem" ? "on-prem" : row.placement}</Item>
        <Item label="Status">
          <span aria-hidden="true">{s.icon} </span>
          {s.label}
          {row.status_reasons.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-sm text-gray-200">
              {row.status_reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
        </Item>
        <Item label="Requests">{row.requests.toLocaleString()}</Item>
        <Item label="Spend">{money(row.spend)}</Item>
        <Item label="Completed without failure">
          {field(c, (v) => percent(v))}
          <span className="block text-xs text-gray-300">
            n={c.n} · {c.failures ?? 0} failures
          </span>
          <span className="block text-xs text-gray-300">
            Policy rejections and cancels have no model, so they are not shown per model.
          </span>
        </Item>
        <Item label="Successful (reported)">
          {field(row.successful_reported, (v) => percent(v))}
          <span className="block text-xs text-gray-300">n={row.successful_reported.n} labelled tasks</span>
        </Item>
        <Item label="Cost per successful task">
          {row.cost_per_successful_task.value === null
            ? insufficientText(row.cost_per_successful_task.n)
            : `${money(row.cost_per_successful_task.value)} (n=${row.cost_per_successful_task.n})`}
        </Item>
        <Item label={`Cost per ${cpt.task_unit}`}>
          attempted {money(cpt.attempted)} · successful {money(cpt.successful)}
          <span className="block text-xs text-gray-300">
            {cpt.n_tasks} {cpt.task_unit}s, {cpt.n_labelled} labelled
          </span>
        </Item>
        <Item label="p95 time">{ms(row.p95_latency_ms)}</Item>
        <Item label="Speed (all applications)">{field(row.speed_tps, tps)}</Item>
        <Item label="TTFT p50 / p95 (all applications)">
          {row.ttft_ms.p50 === null && row.ttft_ms.p95 === null
            ? insufficientText(row.ttft_ms.n)
            : `${ms(row.ttft_ms.p50)} / ${ms(row.ttft_ms.p95)} (n=${row.ttft_ms.n})`}
        </Item>
        <Item label="Cache hit">{percent(row.cache_hit_rate)}</Item>
        <Item label="Errors">{row.errors.toLocaleString()}</Item>
        <Item label="Failovers">{row.failovers.toLocaleString()}</Item>
      </dl>
      <Published row={row} />
    </aside>
  );
}
