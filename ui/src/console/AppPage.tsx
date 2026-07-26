// Application page — the per-application "answer desk". Tabbed console for
// ONE app: Overview/Cost/Privacy/Traffic sections, a Requests tab (list +
// drill-down), and a Settings tab (policy + tokens, reusing AppConfig).
// Owns its own request feed, seeded from the per-app history endpoint and
// kept live via the shared SSE stream filtered to this app's tenant_id.
import { useEffect, useState } from "react";
import { api, adminApi, subscribeStream } from "../api";
import type { RequestSummary, ChatResponse, AdminApp } from "../api";
import { OverviewSection } from "./sections/OverviewSection";
import { CostSection } from "./sections/CostSection";
import { PrivacySection } from "./sections/PrivacySection";
import { TrafficSection } from "./sections/TrafficSection";
import { RequestDrilldown } from "./RequestDrilldown";
import { RequestList } from "../components/RequestList";
import { AppConfig } from "../admin/ApplicationsPanel";

type Tab = "overview" | "cost" | "privacy" | "traffic" | "requests" | "settings";

const REQUEST_CAP = 500;

const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "cost", label: "Cost" },
  { key: "privacy", label: "Privacy" },
  { key: "traffic", label: "Traffic" },
  { key: "requests", label: "Requests" },
  { key: "settings", label: "Settings" },
];

export function AppPage({ appId, onBack }: { appId: string; onBack: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");
  const [app, setApp] = useState<AdminApp | null>(null);
  const [requests, setRequests] = useState<RequestSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<ChatResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // App metadata (name, residency) for the header.
  useEffect(() => {
    let cancelled = false;
    adminApi.apps()
      .then((rows) => !cancelled && setApp(rows.find((a) => a.id === appId) ?? null))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [appId]);

  // Seed this app's requests + subscribe to the live stream, filtered to
  // this tenant only. Reset selection when the app changes.
  useEffect(() => {
    let cancelled = false;
    setRequests([]);
    setSelectedId(null);

    api.requests(REQUEST_CAP, appId)
      .then((rs) => !cancelled && setRequests(rs))
      .catch(() => {});

    const unsubscribe = subscribeStream((evt) => {
      if (cancelled) return;
      if (evt.type !== "request") return; // drilldown re-fetches full detail on select
      if (evt.summary.tenant_id !== appId) return;
      setRequests((prev) => [evt.summary, ...prev].slice(0, REQUEST_CAP));
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [appId]);

  // Fetch full detail for whichever request is selected.
  useEffect(() => {
    if (!selectedId) {
      setSelectedDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    api.request(selectedId)
      .then((d) => !cancelled && setSelectedDetail(d))
      .catch(() => !cancelled && setSelectedDetail(null))
      .finally(() => !cancelled && setDetailLoading(false));
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const tabBtn = (t: Tab) =>
    `px-3 py-1.5 rounded-md text-sm ${tab === t ? "bg-accent/15 text-accent" : "text-gray-400 hover:text-gray-200"}`;

  return (
    <div className="flex-1 flex flex-col">
      <div className="border-b border-edge px-4 py-3 flex items-center gap-3">
        <button
          onClick={onBack}
          className="text-sm text-gray-400 hover:text-gray-200 shrink-0"
        >
          ← Fleet
        </button>
        <div className="min-w-0">
          <div className="text-sm font-semibold text-gray-100 truncate">{app?.name ?? appId}</div>
          <div className="text-xs text-gray-500 truncate">
            {appId} · {app?.default_residency ?? "…"}
          </div>
        </div>
      </div>

      <div className="border-b border-edge px-4 py-2 flex items-center gap-1">
        {TABS.map((t) => (
          <button key={t.key} className={tabBtn(t.key)} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      <div className="flex-1 p-4">
        {tab === "overview" && <OverviewSection requests={requests} />}
        {tab === "cost" && <CostSection requests={requests} />}
        {tab === "privacy" && <PrivacySection requests={requests} />}
        {tab === "traffic" && <TrafficSection requests={requests} />}
        {tab === "requests" && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
            <RequestList requests={requests} selectedId={selectedId} onSelect={setSelectedId} />
            <RequestDrilldown detail={selectedDetail} loading={detailLoading} />
          </div>
        )}
        {tab === "settings" && <AppConfig appId={appId} key={appId} />}
      </div>
    </div>
  );
}
