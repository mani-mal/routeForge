// Per-application usage: totals, savings, tier/model distribution.
import { useEffect, useState } from "react";
import { adminApi, TIER_COLOR } from "../api";
import type { AdminApp, UsageSummary } from "../api";
import { Panel, StatTile, formatMs, formatUsd } from "../components/common";

function Distribution({ title, data, colorFor }: {
  title: string;
  data: Record<string, number>;
  colorFor?: (k: string) => string;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((n, [, v]) => n + v, 0) || 1;
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">{title}</div>
      {entries.length === 0 && <div className="text-sm text-gray-600">no data</div>}
      <div className="flex flex-col gap-1">
        {entries.map(([k, v]) => (
          <div key={k} className="flex items-center gap-2 text-xs">
            <span className="w-28 truncate text-gray-300">{k}</span>
            <div className="flex-1 h-2 rounded bg-ink overflow-hidden">
              <div className="h-full" style={{ width: `${(v / total) * 100}%`, backgroundColor: colorFor?.(k) ?? "#38bdf8" }} />
            </div>
            <span className="w-8 text-right text-gray-500">{v}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function UsagePanel() {
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [appId, setAppId] = useState<string>("");
  const [hours, setHours] = useState<number>(0);
  const [usage, setUsage] = useState<UsageSummary | null>(null);

  useEffect(() => { adminApi.apps().then(setApps).catch(() => {}); }, []);

  useEffect(() => {
    let cancelled = false;
    adminApi.usage(appId || undefined, hours || undefined)
      .then((u) => !cancelled && setUsage(u))
      .catch(() => !cancelled && setUsage(null));
    return () => { cancelled = true; };
  }, [appId, hours]);

  const savedPct = usage && usage.baseline_usd > 0
    ? (((usage.baseline_usd - usage.total_usd) / usage.baseline_usd) * 100).toFixed(1)
    : "0.0";

  return (
    <Panel
      title="Usage"
      right={
        <div className="flex items-center gap-2 text-xs">
          <select value={appId} onChange={(e) => setAppId(e.target.value)}
            className="rounded bg-ink border border-edge px-2 py-1 text-gray-100">
            <option value="">all applications</option>
            {apps.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}
            className="rounded bg-ink border border-edge px-2 py-1 text-gray-100">
            <option value={0}>all time</option>
            <option value={24}>last 24h</option>
            <option value={1}>last 1h</option>
          </select>
        </div>
      }
    >
      {!usage ? (
        <p className="text-sm text-gray-500">No usage yet.</p>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <StatTile label="requests" value={String(usage.count)} sub={`${usage.rejected_count} rejected`} />
            <StatTile label="spend" value={formatUsd(usage.total_usd)} sub={`vs ${formatUsd(usage.baseline_usd)} baseline`} />
            <StatTile label="saved" value={`${savedPct}%`} sub="vs all-frontier" />
            <StatTile label="tokens" value={usage.total_tokens.toLocaleString()} sub={`${usage.prompt_tokens}in / ${usage.completion_tokens}out`} />
            <StatTile label="p50 latency" value={formatMs(usage.latency_p50_ms)} />
            <StatTile label="p95 latency" value={formatMs(usage.latency_p95_ms)} />
            <StatTile label="on-prem" value={String(usage.onprem_count)} sub="kept local" />
            <StatTile label="rejected" value={String(usage.rejected_count)} sub="policy/quota" />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Distribution title="by tier" data={usage.tier_distribution} colorFor={(k) => TIER_COLOR[k] ?? TIER_COLOR["?"]} />
            <Distribution title="by model" data={usage.model_distribution} />
          </div>
          {!appId && <Distribution title="by application" data={usage.application_distribution} />}
        </div>
      )}
    </Panel>
  );
}
