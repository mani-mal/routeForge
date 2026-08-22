// Small shared UI primitives reused across panels — kept boring on purpose.
import type { ReactNode } from "react";

export function Panel({
  title,
  right,
  children,
  className = "",
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`bg-panel border border-edge rounded-lg p-4 flex flex-col gap-3 ${className}`}>
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-xs font-display uppercase tracking-[0.12em] text-gray-400">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

// Tier dot reads the Tailwind tier classes (bg-cheap/bg-mid/bg-frontier), which
// resolve to the tokens.ts hexes — one fewer TIER_COLOR consumer, no inline style.
const TIER_DOT_CLASS: Record<string, string> = {
  cheap: "bg-cheap",
  mid: "bg-mid",
  frontier: "bg-frontier",
};

export function TierDot({ tier }: { tier: string }) {
  const cls = TIER_DOT_CLASS[tier] ?? "bg-gray-600";
  return <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${cls}`} />;
}

export function StatTile({ label, value, sub, info }: { label: string; value: string; sub?: string; info?: string }) {
  return (
    <div className="bg-ink/60 border border-edge rounded-md px-3 py-2 flex flex-col gap-0.5 min-w-0">
      <span className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-gray-500">
        {label}
        {info && (
          <span
            tabIndex={0}
            role="note"
            aria-label={info}
            title={info}
            className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-edge text-[9px] text-gray-500 cursor-help hover:text-gray-300 hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            ?
          </span>
        )}
      </span>
      <span className="text-xl font-bold font-mono text-gray-100 truncate">{value}</span>
      {sub && <span className="text-xs text-gray-500 truncate">{sub}</span>}
    </div>
  );
}

export function formatUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return `$${v.toFixed(4)}`;
}

// Per-request "answer" figures: a sub-$0.00005 cost rounds to "$0.0000" at 4 dp
// and reads as *free*, which is a display artifact, not a claim. For those
// prominent per-request lines, fall back to a sub-cent unit (e.g. "0.003¢").
// Aggregate totals keep formatUsd's 4 dp — there the magnitudes are real.
export function formatUsdPrecise(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (v > 0 && v < 0.0001) {
    const cents = v * 100;
    return cents < 0.001 ? "<0.001¢" : `${cents.toFixed(3)}¢`;
  }
  return `$${v.toFixed(4)}`;
}

export function formatMs(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return `${Math.round(v)} ms`;
}

export function formatPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return `${v.toFixed(1)}%`;
}

// 2-3 significant figures — these are intelligence-density ratios (quality
// per 1k tokens / per $), not currency, so formatUsd/formatPct don't fit.
export function formatDensity(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v) || v === 0) return "–";
  if (v >= 100) return v.toFixed(0);
  if (v >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

// The one honesty caveat every quality-axis figure in the console must carry
// (Task 2 — quality/accuracy-tradeoff axis): "quality" here is a tier-quality
// proxy (cheap 0.5 / mid 0.75 / frontier 1.0) or a calibrated/customer/published
// score when set (app/routing/quality.py's provenance ladder) — NEVER
// ground-truth accuracy. Reused verbatim so the caveat can't drift per call site.
export const QUALITY_CAVEAT =
  "quality is a tier proxy / calibrated — not measured accuracy; needs a per-model quality score";

// A compact "provenance: N% tier_default" note from a quality_source_distribution
// (app/apps/store.py, app/api/history.py). Picks the most common source so the
// note stays short; returns null when there's nothing to show yet.
export function provenanceNote(dist: Record<string, number> | undefined | null): string | null {
  if (!dist) return null;
  const entries = Object.entries(dist);
  const total = entries.reduce((a, [, n]) => a + n, 0);
  if (total === 0) return null;
  const [source, count] = entries.reduce((best, cur) => (cur[1] > best[1] ? cur : best));
  return `provenance: ${((count / total) * 100).toFixed(0)}% ${source}`;
}
