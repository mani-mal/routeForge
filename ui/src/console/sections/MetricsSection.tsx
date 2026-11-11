// Metrics section — the per-application team-lead view: spend against a monthly
// budget, cost per successful task, reported success, p95 task time, where the
// money went, which models served the app and why a pricier one was used.
// Server-owned figures only: risk, forecasts and shares are rendered as given,
// never recomputed here. Definitions live in docs/reference/metrics-api.md.
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { adminApi } from "../../api";
import type { AppWindowView, MetricsApp, MetricsModels } from "../../api";
import { Panel } from "../../components/common";
import { ModelTable } from "../../admin/models/ModelTable";
import { change, field, insufficientText, money, ms, percent } from "../../admin/models/format";

const WINDOWS: { hours: number; label: string }[] = [
  { hours: 24, label: "24 h" },
  { hours: 168, label: "7 d" },
  { hours: 720, label: "30 d" },
  { hours: 2160, label: "90 d" },
];

const SELECT =
  "bg-ink border border-edge rounded-md px-2 py-1.5 text-sm text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";

const GUIDE_URL = "https://github.com/mani-mal/routeForge/blob/main/docs/guides/reporting-task-success.md";
const GUIDE_PATH = "docs/guides/reporting-task-success.md";

const RISK: Record<string, { icon: string; label: string }> = {
  low: { icon: "✓", label: "risk: low" },
  watch: { icon: "⚠", label: "risk: watch" },
  high: { icon: "✕", label: "risk: high" },
};

const REASON_LABEL: Record<string, string> = {
  failover: "cheaper model failed",
  escalation: "confidence escalation",
  placement: "placement rule",
  risk_floor: "risk floor",
  quality_floor: "quality floor",
  length_tier: "request length",
  only_eligible: "only eligible model",
  no_failover: "only eligible endpoint failed",
  other: "other",
};

function reasonLabel(r: string): string {
  return REASON_LABEL[r] ?? r.replace(/_/g, " ");
}

function Card({
  label,
  tip,
  children,
  delta,
}: {
  label: string;
  tip: string;
  children: ReactNode;
  delta?: string | null;
}) {
  return (
    <div className="bg-ink/60 border border-edge rounded-md px-3 py-2 flex flex-col gap-1 min-w-0">
      <span className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-gray-400">
        {label}
        <span
          tabIndex={0}
          role="note"
          aria-label={tip}
          title={tip}
          className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-edge text-[9px] text-gray-400 cursor-help hover:text-gray-200 hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          ?
        </span>
      </span>
      {children}
      {delta && <span className="text-xs text-gray-400">{delta}</span>}
    </div>
  );
}

function Share({ label, share, detail }: { label: string; share: number; detail: string }) {
  const pct = Math.max(0, Math.min(1, share)) * 100;
  return (
    <li className="flex flex-col gap-0.5">
      <div className="flex justify-between gap-3 text-sm text-gray-100">
        <span className="truncate">{label}</span>
        <span className="font-mono shrink-0">
          {percent(share)} <span className="text-gray-400">({detail})</span>
        </span>
      </div>
      <div className="h-1.5 rounded bg-edge" aria-hidden="true">
        <div className="h-1.5 rounded bg-accent" style={{ width: `${pct}%` }} />
      </div>
    </li>
  );
}

function BudgetCard({ cur }: { cur: AppWindowView }) {
  const b = cur.budget;
  const tip =
    "Spend this calendar month (UTC) against the monthly budget set in Settings, with a month-end projection (month-to-date / elapsed days x days in month, at least 1 day). Display only: the budget blocks nothing. Independent of the window.";
  if (b.monthly_budget_usd === null) {
    return (
      <Card label="Spend / monthly budget" tip={tip}>
        <span className="text-xl font-bold font-mono text-gray-100">{money(b.month_to_date_usd)}</span>
        <span className="text-xs text-gray-300">No monthly budget set — set one in Settings</span>
      </Card>
    );
  }
  const risk = b.risk ? RISK[b.risk] : null;
  return (
    <Card label="Spend / monthly budget" tip={tip}>
      <span className="text-xl font-bold font-mono text-gray-100">
        {money(b.month_to_date_usd)} <span className="text-sm font-normal text-gray-300">of {money(b.monthly_budget_usd)} used</span>
      </span>
      <span className="text-xs text-gray-200">
        projected month-end {money(b.forecast_usd)}
        {risk && (
          <>
            {" · "}
            <span aria-hidden="true">{risk.icon} </span>
            {risk.label}
          </>
        )}
      </span>
    </Card>
  );
}

function Cards({ cur, prev }: { cur: AppWindowView; prev: AppWindowView }) {
  const cpt = cur.cost_per_task;
  const unit = cpt.task_unit === "request" ? "request" : "task";
  const succ = cur.successful_reported;
  const p95 = cur.p95_task_time_ms;
  const p95Unit = p95.task_unit === "request" ? "per request" : "per task";
  const cptValue = cpt.successful_insufficient
    ? insufficientText(cpt.n_labelled)
    : cpt.successful === null
      ? "no successful tasks reported"
      : money(cpt.successful);
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
      <BudgetCard cur={cur} />
      <Card
        label="Cost per successful task"
        tip="Total cost of labelled tasks divided by the tasks reported successful, from task_success labels the application sends. Needs at least 20 labelled tasks. A task is a trajectory (or a single request when the app sends no trajectory id)."
        delta={change(cpt.successful, prev.cost_per_task.successful)?.text}
      >
        <span className="text-xl font-bold font-mono text-gray-100">{cptValue}</span>
        <span className="text-xs text-gray-300">
          attempted {money(cpt.attempted)} per {unit}
        </span>
      </Card>
      <Card
        label="Successful (reported)"
        tip="Share of labelled tasks the application reported as successful (task_success = 1; the latest label wins). Reported by your application (or entered by an admin in the console), not measured by RouteForge. Needs at least 20 labelled tasks."
        delta={succ ? change(succ.value, prev.successful_reported?.value)?.text : null}
      >
        {succ ? (
          <span className="text-xl font-bold font-mono text-gray-100">{field(succ, (v) => percent(v))}</span>
        ) : (
          <span className="text-sm text-gray-200">
            No reports yet — see{" "}
            <a className="underline text-accent" href={GUIDE_URL} target="_blank" rel="noreferrer">
              Reporting task success
            </a>{" "}
            ({GUIDE_PATH} in the repo)
          </span>
        )}
        <span className="text-xs text-gray-300">
          completed without failure: {field(cur.completed, (v) => percent(v))}
        </span>
      </Card>
      <Card
        label="p95 task time"
        tip="95th percentile of task duration: time from the start of the first request to the end of the last, over tasks with at least one successful request. Without trajectory ids it is p95 request latency. Needs at least 20 tasks (30 requests)."
        delta={change(p95.value, prev.p95_task_time_ms.value)?.text}
      >
        <span className="text-xl font-bold font-mono text-gray-100">
          {p95.value === null ? insufficientText(p95.n) : ms(p95.value)}
        </span>
        <span className="text-xs text-gray-300">{p95Unit}</span>
      </Card>
    </div>
  );
}

export function MetricsSection({ appId }: { appId: string }) {
  const [hours, setHours] = useState(720);
  const [data, setData] = useState<MetricsApp | null>(null);
  const [models, setModels] = useState<MetricsModels | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([adminApi.metricsApp(appId, hours), adminApi.metricsModels({ hours, applicationId: appId })])
      .then(([a, m]) => {
        if (cancelled) return;
        setData(a);
        setModels(m);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Could not load metrics");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [appId, hours]);

  const cur = data?.current;
  const totalReq = cur ? cur.models_used.reduce((n, m) => n + m.requests, 0) : 0;

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title="Metrics"
        right={
          <label className="flex items-center gap-2 text-xs text-gray-300">
            Window
            <select className={SELECT} value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              {WINDOWS.map((w) => (
                <option key={w.hours} value={w.hours}>
                  Last {w.label}
                </option>
              ))}
            </select>
          </label>
        }
      >
        {error && (
          <p role="alert" className="text-sm text-red-300">
            Could not load metrics: {error}
          </p>
        )}
        {loading && !data && <p className="text-sm text-gray-400">Loading metrics…</p>}
        {loading && data && <p className="text-xs text-gray-400">Refreshing…</p>}
        {data && cur && !error && <Cards cur={cur} prev={data.previous} />}
      </Panel>

      {data && cur && !error && (
        <>
          <Panel title="Spend by workload">
            {cur.spend_by_workload.length === 0 ? (
              <p className="text-sm text-gray-300">No spend in this window.</p>
            ) : (
              <ul className="flex flex-col gap-1 text-sm text-gray-100">
                {cur.spend_by_workload.map((w) => (
                  <li key={w.workload ?? "__untagged"} className="flex justify-between gap-3">
                    <span>{w.workload ?? "untagged"}</span>
                    <span className="font-mono">{money(w.usd)}</span>
                  </li>
                ))}
                <li className="flex justify-between gap-3 border-t border-edge pt-1">
                  <span>Retries and failovers</span>
                  <span className="font-mono">{money(cur.retries_and_failovers_usd)}</span>
                </li>
              </ul>
            )}
            <p className="text-xs text-gray-400">
              Retries and failovers is the cost of requests rerouted after a failure or escalated; it overlaps the
              workload lines above and is not added to them.
            </p>
          </Panel>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
            <Panel title="Models used">
              {cur.models_used.length === 0 ? (
                <p className="text-sm text-gray-300">No routed requests in this window.</p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {cur.models_used.map((m) => (
                    <Share key={m.model} label={m.model} share={m.share} detail={`${m.requests} of ${totalReq} requests`} />
                  ))}
                </ul>
              )}
              <p className="text-xs text-gray-400">
                Share of this application's routed requests per model. A request that failed over counts once, under the
                model that ended it.
              </p>
            </Panel>
            <Panel title="Why the pricier model was used">
              {cur.route_reasons.length === 0 ? (
                <p className="text-sm text-gray-300">No routing reasons recorded in this window.</p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {cur.route_reasons.map((r) => (
                    <Share key={r.reason} label={reasonLabel(r.reason)} share={r.share} detail={`${r.count} requests`} />
                  ))}
                </ul>
              )}
              <p className="text-xs text-gray-400">
                The reason the router recorded for each choice. Requests with no recorded reason are left out.
              </p>
            </Panel>
          </div>

          {models && (
            <Panel title="Models">
              <ModelTable rows={models.models} />
            </Panel>
          )}
        </>
      )}
    </div>
  );
}
