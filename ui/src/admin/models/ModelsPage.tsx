// Models station: filters, outcome cards, alerts, per-model table and
// diagnostics, backed by GET /admin/metrics/{summary,models}.
import { useEffect, useState } from "react";
import { adminApi } from "../../api";
import type { AdminApp, MetricsModels, MetricsSummary } from "../../api";
import { Panel } from "../../components/common";
import { OutcomeCards } from "./OutcomeCards";
import { AlertsList } from "./AlertsList";
import { stamp } from "./format";
import { ModelTable } from "./ModelTable";
import { ModelChart } from "./ModelChart";
import { Diagnostics } from "./Diagnostics";

const WINDOWS: { hours: number; label: string }[] = [
  { hours: 24, label: "24 h" },
  { hours: 168, label: "7 d" },
  { hours: 720, label: "30 d" },
  { hours: 2160, label: "90 d" },
];

const SELECT =
  "bg-ink border border-edge rounded-md px-2 py-1.5 text-sm text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";

export function ModelsPage() {
  const [hours, setHours] = useState(720);
  const [applicationId, setApplicationId] = useState("");
  const [workload, setWorkload] = useState("");
  const [placement, setPlacement] = useState("");
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [workloads, setWorkloads] = useState<string[]>([]);
  const [summary, setSummary] = useState<MetricsSummary | null>(null);
  const [models, setModels] = useState<MetricsModels | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    adminApi.apps().then(setApps).catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const params = {
      hours,
      applicationId: applicationId || undefined,
      workload: workload || undefined,
      placement: placement || undefined,
    };
    Promise.all([adminApi.metricsSummary(params), adminApi.metricsModels(params)])
      .then(([s, m]) => {
        if (cancelled) return;
        setSummary(s);
        setModels(m);
        // Keep the workload choices stable while a workload filter is active
        // (the server computes the list before filtering, but be defensive).
        setWorkloads(s.current.workloads); // computed by the server before the workload filter
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
  }, [hours, applicationId, workload, placement]);

  // Empty only when the window has no usage AND no outcome rows (the server decides):
  // a window of nothing but policy rejections still has something to show.
  const empty = summary !== null && summary.current.empty;

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title="Models"
        right={
          summary && (
            <span className="text-xs text-gray-300">RouteForge data: generated {stamp(summary.generated_at)}</span>
          )
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-gray-300">
            Application
            <select className={SELECT} value={applicationId} onChange={(e) => setApplicationId(e.target.value)}>
              <option value="">All applications</option>
              {apps.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-300">
            Workload
            <select className={SELECT} value={workload} onChange={(e) => setWorkload(e.target.value)}>
              <option value="">All workloads</option>
              {workloads.map((w) => (
                <option key={w} value={w}>
                  {w}
                </option>
              ))}
              {workload && !workloads.includes(workload) && <option value={workload}>{workload}</option>}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-300">
            Placement
            <select className={SELECT} value={placement} onChange={(e) => setPlacement(e.target.value)}>
              <option value="">On-prem and cloud</option>
              <option value="onprem">On-prem</option>
              <option value="cloud">Cloud</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-300">
            Window
            <select className={SELECT} value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              {WINDOWS.map((w) => (
                <option key={w.hours} value={w.hours}>
                  Last {w.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </Panel>

      {error && (
        <p role="alert" className="text-sm text-red-300">
          Could not load model metrics: {error}
        </p>
      )}
      {loading && !summary && <p className="text-sm text-gray-400">Loading model metrics…</p>}
      {loading && summary && <p className="text-xs text-gray-400">Refreshing…</p>}

      {summary && models && !error && (
        <>
          <AlertsList alerts={summary.alerts} />
          {empty ? (
            <Panel title="No data">
              <p className="text-sm text-gray-300">
                No requests match these filters in the selected window. Widen the window or clear a filter.
              </p>
            </Panel>
          ) : (
            <>
              <OutcomeCards data={summary} />
              <Panel title="Models">
                <ModelTable rows={models.models} />
              </Panel>
              <Panel title="Cost vs success">
                <ModelChart rows={models.models} />
              </Panel>
              <Diagnostics routeReasons={summary.current.route_reasons} rows={models.models} />
            </>
          )}
        </>
      )}
    </div>
  );
}
