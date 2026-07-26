// Serving Plane demo UI — wires the four panels + control bar to the
// Phase-6a backend. See src/api.ts for the contract and
// .claude/skills/demo-ui/SKILL.md for the design brief this follows.
import { useEffect, useState } from "react";
import { api, subscribeStream } from "./api";
import type { ChatResponse, Controls, RequestSummary, Stats, TraceRecord } from "./api";
import { CostDashboard } from "./components/CostDashboard";
import { CompressionView } from "./components/CompressionView";
import { DecisionInspector } from "./components/DecisionInspector";
import { FlowVisualizer } from "./components/FlowVisualizer";
import { RequestList } from "./components/RequestList";
import { AdminConsole } from "./admin/AdminConsole";
import { RouteForgeLogo, CdwMark, ThemeToggle } from "./components/BrandLogo";
import { useTheme } from "./theme";

type View = "live" | "admin";

const REQUEST_CAP = 200;
const LIVE_TRACE_CAP = 50;

export default function App() {
  const [requests, setRequests] = useState<RequestSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedStep, setSelectedStep] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<ChatResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [stats, setStats] = useState<Stats | null>(null);
  const [controls, setControls] = useState<Controls | null>(null);
  const [liveTraces, setLiveTraces] = useState<Record<string, TraceRecord[]>>({});
  const [connected, setConnected] = useState(false);
  const [togglingHeadroom, setTogglingHeadroom] = useState(false);
  const [view, setView] = useState<View>("live");
  const [theme, toggleTheme] = useTheme();

  // Initial load + live stream subscription.
  useEffect(() => {
    let cancelled = false;

    api.requests().then((rs) => !cancelled && setRequests(rs)).catch(() => {});
    api.stats().then((s) => !cancelled && setStats(s)).catch(() => {});
    api.controls().then((c) => !cancelled && setControls(c)).catch(() => {});

    const unsubscribe = subscribeStream((evt) => {
      if (evt.type === "request") {
        setRequests((prev) => [evt.summary, ...prev].slice(0, REQUEST_CAP));
        setSelectedId((curr) => curr ?? evt.summary.id);
        api.stats().then(setStats).catch(() => {});
      } else if (evt.type === "step") {
        setLiveTraces((prev) => {
          const next = { ...prev };
          next[evt.request_id] = [...(next[evt.request_id] ?? []), evt.record];
          const keys = Object.keys(next);
          if (keys.length > LIVE_TRACE_CAP) delete next[keys[0]];
          return next;
        });
        setSelectedId((curr) => curr ?? evt.request_id);
      }
    }, (isConnected) => setConnected(isConnected));

    return () => {
      cancelled = true;
      unsubscribe();
      setConnected(false);
    };
  }, []);

  // Cheap poll as a backstop in case SSE drops a stats refresh.
  useEffect(() => {
    const t = setInterval(() => {
      api.stats().then(setStats).catch(() => {});
    }, 3000);
    return () => clearInterval(t);
  }, []);

  // Fetch the full trace for whichever request is selected.
  useEffect(() => {
    setSelectedStep(null); // switching requests clears the focused step
    if (!selectedId) {
      setSelectedDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    api
      .request(selectedId)
      .then((d) => !cancelled && setSelectedDetail(d))
      .catch(() => !cancelled && setSelectedDetail(null))
      .finally(() => !cancelled && setDetailLoading(false));
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  async function handleToggleHeadroom() {
    if (!controls) return;
    setTogglingHeadroom(true);
    try {
      const next = await api.setControls(!controls.headroom_enabled);
      setControls(next);
    } catch {
      // demo-safe: leave controls as-is on failure
    } finally {
      setTogglingHeadroom(false);
    }
  }

  const selectedSummary = requests.find((r) => r.id === selectedId) ?? null;
  const liveTrace = selectedId ? liveTraces[selectedId] ?? [] : [];
  const effectiveTrace = selectedDetail?.trace ?? liveTrace;
  const allowedProviders = selectedDetail?.policy.allowed_providers ?? null;
  const onpremOnly = selectedDetail?.policy.onprem_only ?? selectedSummary?.onprem_only ?? null;
  const hops =
    selectedSummary?.hops ??
    effectiveTrace.filter((r) => r.step === "router" || r.step === "agent_hop").length;

  return (
    <div className="min-h-screen bg-ink text-gray-100 flex flex-col">
      <header className="border-b border-edge px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <RouteForgeLogo />
          <nav className="flex items-center gap-1">
            <button
              onClick={() => setView("live")}
              className={`px-3 py-1 rounded-md text-sm ${view === "live" ? "bg-accent/15 text-accent" : "text-gray-400 hover:text-gray-200"}`}
            >
              Live
            </button>
            <button
              onClick={() => setView("admin")}
              className={`px-3 py-1 rounded-md text-sm ${view === "admin" ? "bg-accent/15 text-accent" : "text-gray-400 hover:text-gray-200"}`}
            >
              Admin
            </button>
          </nav>
        </div>
        <div className="flex items-center gap-4 text-sm text-gray-400">
          {view === "live" && <span>{requests.length} requests</span>}
          <span className="flex items-center gap-2">
            <span className={`h-2.5 w-2.5 rounded-full ${connected ? "bg-cheap" : "bg-gray-600"}`} />
            {connected ? "live" : "disconnected"}
          </span>
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          <span className="flex items-center gap-2 pl-4 border-l border-edge">
            <span className="text-[10px] uppercase tracking-wide text-gray-500">powered by</span>
            <CdwMark className="h-5 w-auto" />
          </span>
        </div>
      </header>

      {view === "admin" ? (
        <AdminConsole />
      ) : (
        <main className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-4 p-4 items-start">
            <div className="lg:col-span-4 flex flex-col gap-4">
              <FlowVisualizer
                trace={effectiveTrace}
                allowedProviders={allowedProviders}
                onpremOnly={onpremOnly}
                hops={hops}
                selectedStep={selectedStep}
                onSelectStep={(k) => setSelectedStep((curr) => (curr === k ? null : k))}
              />
              <RequestList requests={requests} selectedId={selectedId} onSelect={setSelectedId} />
            </div>

            <div className="lg:col-span-4 flex flex-col gap-4">
              <DecisionInspector
                requestId={selectedId}
                detail={selectedDetail}
                loading={detailLoading}
                selectedStep={selectedStep}
                onClearStep={() => setSelectedStep(null)}
                hops={hops}
              />
              <CompressionView
                detail={selectedDetail}
                headroomEnabled={controls?.headroom_enabled ?? null}
                onToggleHeadroom={handleToggleHeadroom}
                toggling={togglingHeadroom}
              />
            </div>

            <div className="lg:col-span-4">
              <CostDashboard stats={stats} requests={requests} />
            </div>
        </main>
      )}
    </div>
  );
}
