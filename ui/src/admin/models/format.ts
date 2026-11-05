// Shared formatters for the Models station. Insufficient data is always text,
// never a blank or a zero (a zero would read as a measurement).
import type { ComparabilityValue, MetricField } from "../../api";

export function insufficientText(n: number): string {
  return `insufficient data (n=${n})`;
}

export function money(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (v === 0) return "$0.00";
  if (Math.abs(v) < 0.01) return `$${v.toFixed(6)}`;
  if (Math.abs(v) < 100) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

export function percent(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return `${(v * 100).toFixed(digits)}%`;
}

export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(0)} ms`;
}

export function tps(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return `${v.toFixed(1)} tok/s`;
}

/** A MetricField rendered with `fmt`, or the insufficient-data text. */
export function field(f: MetricField | null | undefined, fmt: (v: number) => string): string {
  if (!f) return "–";
  if (f.value === null || f.value === undefined) return insufficientText(f.n);
  return fmt(f.value);
}

export interface Change {
  arrow: "▲" | "▼" | "▬";
  text: string; // e.g. "▲ 12.0% vs previous period"
}

/** Change vs the previous window. Null when either side has no figure or the
 *  previous is zero (a percent of zero is undefined). The arrow carries the
 *  direction, so the meaning never rests on colour. */
export function change(cur: number | null | undefined, prev: number | null | undefined): Change | null {
  if (cur === null || cur === undefined || prev === null || prev === undefined) return null;
  if (prev === 0) return null;
  const pct = ((cur - prev) / Math.abs(prev)) * 100;
  if (Math.abs(pct) < 0.05) return { arrow: "▬", text: "▬ no change vs previous period" };
  const arrow = pct > 0 ? "▲" : "▼";
  return { arrow, text: `${arrow} ${Math.abs(pct).toFixed(1)}% vs previous period` };
}

export function stamp(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export const STATUS_LABEL: Record<string, { icon: string; label: string }> = {
  healthy: { icon: "✓", label: "Healthy" },
  insufficient_data: { icon: "…", label: "Insufficient data" },
  cache_regression: { icon: "⚠", label: "Cache regression" },
  performance_degradation: { icon: "⚠", label: "Performance degradation" },
  cost_anomaly: { icon: "⚠", label: "Cost anomaly" },
  price_drift: { icon: "⚠", label: "Price drift" },
};

export function statusLabel(s: string): { icon: string; label: string } {
  return STATUS_LABEL[s] ?? { icon: "•", label: s.replace(/_/g, " ") };
}

/** AA publishes TTFT in seconds; the local fields are milliseconds. */
export function secs(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v.toFixed(2)} s`;
}

/** A published (AA) number, "—" when not published (never 0). */
export function pub(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

export function pubMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return money(v);
}

export const COMPARABILITY_LABEL: Record<ComparabilityValue, { icon: string; label: string }> = {
  comparable: { icon: "✓", label: "Comparable" },
  partially_comparable: { icon: "◐", label: "Partially comparable" },
  not_comparable: { icon: "✕", label: "Not comparable" },
  insufficient_local_data: { icon: "…", label: "Insufficient local data" },
};

export function comparabilityLabel(v: string): { icon: string; label: string } {
  return COMPARABILITY_LABEL[v as ComparabilityValue] ?? { icon: "•", label: v.replace(/_/g, " ") };
}
