// Cross-app usage report — an admin-facing table backed by the durable
// per-application rollup (GET /admin/usage/by-application). Unlike the live
// FleetOverview (in-process, resets on restart), this reads DB-backed totals,
// so it's the source of truth for "how has each app used the fleet".
import { useEffect, useState } from "react";
import { adminApi } from "../api";
import type { AdminApp, UsageByApp } from "../api";
import { Panel, formatUsd, formatPct, formatDensity, provenanceNote, QUALITY_CAVEAT } from "../components/common";

interface JoinedRow {
  appId: string;
  appName: string;
  count: number;
  total_usd: number;
  baseline_usd: number;
  saved_usd: number;
  onprem_pct: number;
  top_tier: string;
  // Quality-per-dollar (cost_intelligence_density) + its provenance — a
  // tier-quality PROXY, never measured accuracy (see QUALITY_CAVEAT).
  cost_intelligence_density: number;
  quality_provenance: string | null;
}

function topTier(dist: Record<string, number>): string {
  const entries = Object.entries(dist);
  if (entries.length === 0) return "—";
  return entries.reduce((best, cur) => (cur[1] > best[1] ? cur : best))[0];
}

function joinRows(apps: AdminApp[], usage: UsageByApp[]): JoinedRow[] {
  const usageById = new Map(usage.map((u) => [u.application_id, u]));
  return apps.map((app) => {
    const u = usageById.get(app.id);
    const count = u?.count ?? 0;
    const total_usd = u?.total_usd ?? 0;
    const baseline_usd = u?.baseline_usd ?? 0;
    const onprem_count = u?.onprem_count ?? 0;
    return {
      appId: app.id,
      appName: app.name,
      count,
      total_usd,
      baseline_usd,
      saved_usd: baseline_usd - total_usd,
      onprem_pct: count > 0 ? (onprem_count / count) * 100 : 0,
      top_tier: topTier(u?.tier_distribution ?? {}),
      cost_intelligence_density: u?.cost_intelligence_density ?? 0,
      quality_provenance: provenanceNote(u?.quality_source_distribution),
    };
  });
}

const GRID = "grid grid-cols-[minmax(140px,2fr)_80px_90px_90px_90px_80px_80px_100px] items-center gap-3";

export function CrossAppReport() {
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [usage, setUsage] = useState<UsageByApp[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    Promise.all([adminApi.apps(), adminApi.usageByApplication()])
      .then(([a, u]) => {
        if (cancelled) return;
        setApps(a);
        setUsage(u.apps);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const rows = joinRows(apps, usage);

  return (
    <Panel title="Cross-app usage">
      {loading ? (
        <p className="text-sm text-gray-500">Loading usage…</p>
      ) : apps.length === 0 ? (
        <p className="text-sm text-gray-500">No applications yet.</p>
      ) : (
        <div className="flex flex-col gap-1.5">
          <div className={`${GRID} px-3`}>
            <span className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-500">
              App
            </span>
            <span className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-500">
              Requests
            </span>
            <span className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-500">
              Spend
            </span>
            <span className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-500">
              Baseline
            </span>
            <span className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-500">
              Saved
            </span>
            <span className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-500">
              On-prem %
            </span>
            <span className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-500">
              Top tier
            </span>
            <span
              className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-500 cursor-help"
              title={QUALITY_CAVEAT}
            >
              Quality/$ *
            </span>
          </div>

          {rows.map((row) => (
            <div
              key={row.appId}
              className={`${GRID} w-full rounded-md border border-edge bg-ink/40 px-3 py-2`}
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-gray-100">{row.appName}</div>
                <div className="truncate text-xs font-mono text-gray-500">{row.appId}</div>
              </div>
              <span className="text-sm font-mono text-gray-300">{row.count.toLocaleString()}</span>
              <span className="text-sm font-mono text-gray-300">{formatUsd(row.total_usd)}</span>
              <span className="text-sm font-mono text-gray-300">{formatUsd(row.baseline_usd)}</span>
              <span className="text-sm font-mono text-gray-300">{formatUsd(row.saved_usd)}</span>
              <span className="text-sm font-mono text-gray-300">{formatPct(row.onprem_pct)}</span>
              <span className="text-sm font-mono text-gray-300">{row.top_tier}</span>
              <span
                className="text-sm font-mono text-gray-300 truncate"
                title={row.quality_provenance ? `${QUALITY_CAVEAT} (${row.quality_provenance})` : QUALITY_CAVEAT}
              >
                {formatDensity(row.cost_intelligence_density)}
              </span>
            </div>
          ))}
          <p className="px-3 text-[11px] text-gray-500">* {QUALITY_CAVEAT}</p>
        </div>
      )}
    </Panel>
  );
}
