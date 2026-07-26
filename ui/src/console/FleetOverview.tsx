// Fleet overview — the admin's landing screen. An all-apps aggregate strip
// on top, then a sortable per-application table. Clicking a row drills into
// that app (wired by the caller via `onOpenApp`).
import { useEffect, useState } from "react";
import { api, adminApi, TIER_COLOR } from "../api";
import type { AdminApp, FleetRow } from "../api";
import { Panel, StatTile, TierDot, formatUsd, formatMs } from "../components/common";

const POLL_MS = 3000;
const TIER_ORDER = ["cheap", "mid", "frontier", "?"];

type SortKey = "name" | "requests" | "spend" | "saved" | "pii" | "onprem" | "latency";

interface JoinedRow {
  app: AdminApp;
  count: number;
  total_usd: number;
  saved_usd: number;
  saved_pct: number;
  pii_count: number;
  onprem_count: number;
  latency_p50_ms: number;
  tier_distribution: Record<string, number>;
  warn: boolean;
}

function joinRows(apps: AdminApp[], fleet: FleetRow[]): JoinedRow[] {
  const byApp = new Map(fleet.map((f) => [f.app, f]));
  return apps.map((app) => {
    const f = byApp.get(app.id);
    const total_usd = f?.total_usd ?? 0;
    const baseline_usd = f?.baseline_usd ?? 0;
    const saved_usd = baseline_usd - total_usd;
    const saved_pct = baseline_usd > 0 ? (saved_usd / baseline_usd) * 100 : 0;
    const pii_count = f?.pii_count ?? 0;
    const onprem_count = f?.onprem_count ?? 0;
    return {
      app,
      count: f?.count ?? 0,
      total_usd,
      saved_usd,
      saved_pct,
      pii_count,
      onprem_count,
      latency_p50_ms: f?.latency_p50_ms ?? 0,
      tier_distribution: f?.tier_distribution ?? {},
      warn: pii_count > 0 && onprem_count < pii_count,
    };
  });
}

function sortRows(rows: JoinedRow[], key: SortKey, dir: "asc" | "desc"): JoinedRow[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    switch (key) {
      case "name":
        return sign * a.app.name.localeCompare(b.app.name);
      case "requests":
        return sign * (a.count - b.count);
      case "spend":
        return sign * (a.total_usd - b.total_usd);
      case "saved":
        return sign * (a.saved_pct - b.saved_pct);
      case "pii":
        return sign * (a.pii_count - b.pii_count);
      case "onprem":
        return sign * (a.onprem_count - b.onprem_count);
      case "latency":
        return sign * (a.latency_p50_ms - b.latency_p50_ms);
      default:
        return 0;
    }
  });
}

function TierMiniBar({ dist }: { dist: Record<string, number> }) {
  const total = Object.values(dist).reduce((a, b) => a + b, 0);
  if (total === 0) return <span className="text-xs text-gray-600">–</span>;
  const title = TIER_ORDER.filter((t) => dist[t]).map((t) => `${t}: ${dist[t]}`).join(", ");
  return (
    <div className="flex h-2 w-16 overflow-hidden rounded-sm bg-ink/60" title={title}>
      {TIER_ORDER.map((t) => {
        const v = dist[t] ?? 0;
        if (v <= 0) return null;
        return (
          <div
            key={t}
            style={{ width: `${(v / total) * 100}%`, backgroundColor: TIER_COLOR[t] ?? TIER_COLOR["?"] }}
          />
        );
      })}
    </div>
  );
}

function Th({
  label,
  sortKey,
  activeKey,
  dir,
  onSort,
  className = "",
}: {
  label: string;
  sortKey: SortKey;
  activeKey: SortKey;
  dir: "asc" | "desc";
  onSort: (key: SortKey) => void;
  className?: string;
}) {
  const active = sortKey === activeKey;
  return (
    <button
      onClick={() => onSort(sortKey)}
      className={`flex items-center gap-1 text-left text-[11px] font-semibold uppercase tracking-wide hover:text-gray-300 ${
        active ? "text-gray-200" : "text-gray-500"
      } ${className}`}
    >
      {label}
      {active && <span className="text-[9px]">{dir === "asc" ? "▲" : "▼"}</span>}
    </button>
  );
}

// Fixed grid template shared by the header row and every data row so columns
// line up. Buttons render `display: grid` fine via these classes.
const GRID =
  "grid grid-cols-[minmax(140px,2fr)_80px_90px_110px_56px_72px_80px_88px_28px] items-center gap-3";

export function FleetOverview({ onOpenApp }: { onOpenApp: (appId: string) => void }) {
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [fleet, setFleet] = useState<FleetRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>("requests");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  useEffect(() => {
    let cancelled = false;

    Promise.all([adminApi.apps(), api.fleet()])
      .then(([a, f]) => {
        if (cancelled) return;
        setApps(a);
        setFleet(f);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    // Fleet stats move fast (live traffic); apps (names/policy) don't need
    // to be re-fetched on the same cadence.
    const t = setInterval(() => {
      api
        .fleet()
        .then((f) => !cancelled && setFleet(f))
        .catch(() => {});
    }, POLL_MS);

    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "name" ? "asc" : "desc");
    }
  }

  const totalRequests = fleet.reduce((a, r) => a + r.count, 0);
  const totalSpend = fleet.reduce((a, r) => a + r.total_usd, 0);
  const totalBaseline = fleet.reduce((a, r) => a + r.baseline_usd, 0);
  const totalSaved = totalBaseline - totalSpend;
  const savedPct = totalBaseline > 0 ? (totalSaved / totalBaseline) * 100 : 0;
  const totalOnprem = fleet.reduce((a, r) => a + r.onprem_count, 0);

  const rows = sortRows(joinRows(apps, fleet), sortKey, sortDir);

  const tierLegend = (
    <div className="flex items-center gap-3 text-xs text-gray-500">
      {TIER_ORDER.filter((t) => t !== "?").map((t) => (
        <span key={t} className="flex items-center gap-1">
          <TierDot tier={t} />
          {t}
        </span>
      ))}
    </div>
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatTile label="Requests" value={totalRequests.toLocaleString()} />
        <StatTile label="Spend" value={formatUsd(totalSpend)} />
        <StatTile
          label="Saved"
          value={formatUsd(totalSaved)}
          sub={totalBaseline > 0 ? `${savedPct.toFixed(1)}% vs. all-frontier` : undefined}
        />
        <StatTile label="Kept on-prem" value={totalOnprem.toLocaleString()} />
      </div>

      <Panel title="Applications" right={tierLegend}>
        {loading ? (
          <p className="text-sm text-gray-500">Loading fleet…</p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-gray-500">No applications yet.</p>
        ) : (
          <div className="flex flex-col gap-1.5">
            <div className={`${GRID} px-3`}>
              <Th label="App" sortKey="name" activeKey={sortKey} dir={sortDir} onSort={toggleSort} />
              <Th label="Requests" sortKey="requests" activeKey={sortKey} dir={sortDir} onSort={toggleSort} />
              <Th label="Spend" sortKey="spend" activeKey={sortKey} dir={sortDir} onSort={toggleSort} />
              <Th label="Saved" sortKey="saved" activeKey={sortKey} dir={sortDir} onSort={toggleSort} />
              <Th label="PII" sortKey="pii" activeKey={sortKey} dir={sortDir} onSort={toggleSort} />
              <Th label="On-prem" sortKey="onprem" activeKey={sortKey} dir={sortDir} onSort={toggleSort} />
              <Th label="p50" sortKey="latency" activeKey={sortKey} dir={sortDir} onSort={toggleSort} />
              <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">Tiers</span>
              <span />
            </div>

            {rows.map((row) => (
              <button
                key={row.app.id}
                onClick={() => onOpenApp(row.app.id)}
                className={`${GRID} w-full rounded-md border border-edge bg-ink/40 px-3 py-2 text-left transition-colors hover:border-gray-600`}
              >
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-gray-100">{row.app.name}</div>
                  <div className="truncate text-xs text-gray-500">{row.app.id}</div>
                </div>
                <span className="text-sm text-gray-300">{row.count.toLocaleString()}</span>
                <span className="text-sm text-gray-300">{formatUsd(row.total_usd)}</span>
                <span className="text-sm text-gray-300">
                  {formatUsd(row.saved_usd)}
                  <span className="ml-1 text-xs text-gray-500">({row.saved_pct.toFixed(0)}%)</span>
                </span>
                <span className="text-sm text-gray-300">{row.pii_count}</span>
                <span className="text-sm text-gray-300">{row.onprem_count}</span>
                <span className="text-sm text-gray-300">{formatMs(row.latency_p50_ms)}</span>
                <TierMiniBar dist={row.tier_distribution} />
                {row.warn ? (
                  <span
                    className="text-amber-400"
                    title="Sensitive traffic not all kept on-prem"
                  >
                    ⚠
                  </span>
                ) : (
                  <span />
                )}
              </button>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}
