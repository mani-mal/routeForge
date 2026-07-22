// Small shared UI primitives reused across panels — kept boring on purpose.
import type { ReactNode } from "react";
import { TIER_COLOR } from "../api";

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
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-400">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

export function TierDot({ tier }: { tier: string }) {
  const color = TIER_COLOR[tier] ?? TIER_COLOR["?"];
  return <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: color }} />;
}

export function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-ink/60 border border-edge rounded-md px-3 py-2 flex flex-col gap-0.5 min-w-0">
      <span className="text-[11px] uppercase tracking-wide text-gray-500">{label}</span>
      <span className="text-xl font-bold text-gray-100 truncate">{value}</span>
      {sub && <span className="text-xs text-gray-500 truncate">{sub}</span>}
    </div>
  );
}

export function formatUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
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
