// Serving Plane demo UI — wires the four panels + control bar to the
// Phase-6a backend. See src/api.ts for the contract and
// .claude/skills/demo-ui/SKILL.md for the design brief this follows.
import { useEffect, useState } from "react";
import { api, SCRIPTED, subscribeStream } from "./api";
import type { ChatResponse, Controls, RequestSummary, Stats, TraceRecord } from "./api";
import { ControlPanel } from "./components/ControlPanel";
import { CostDashboard } from "./components/CostDashboard";
import { CompressionView } from "./components/CompressionView";
import { DecisionInspector } from "./components/DecisionInspector";
import { FlowVisualizer } from "./components/FlowVisualizer";
import { RequestList } from "./components/RequestList";

const REQUEST_CAP = 200;
const LIVE_TRACE_CAP = 50;

export default function App() {
  const [requests, setRequests] = useState<RequestSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<ChatResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [stats, setStats] = useState<Stats | null>(null);
  const [controls, setControls] = useState<Controls | null>(null);
  const [liveTraces, setLiveTraces] = useState<Record<string, TraceRecord[]>>({});
  const [connected, setConnected] = useState(false);
  const [togglingHeadroom, setTogglingHeadroom] = useState(false);
  const [firingIndex, setFiringIndex] = useState<number | null>(null);

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

  async function handleFireScripted(index: number) {
    const s = SCRIPTED[index];
    if (!s) return;
    setFiringIndex(index);
    try {
      await api.chat(s.body, s.apiKey);
    } catch {
      // result also streams via SSE; a failed fetch just means no new event
    } finally {
      setFiringIndex(null);
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
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-bold tracking-tight">Serving Plane</h1>
          <span className="text-xs text-gray-500">Phase-6 demo</span>
        </div>
        <div className="flex items-center gap-4 text-sm text-gray-400">
          <span>{requests.length} requests</span>
          <span className="flex items-center gap-2">
            <span className={`h-2.5 w-2.5 rounded-full ${connected ? "bg-cheap" : "bg-gray-600"}`} />
            {connected ? "live" : "disconnected"}
          </span>
        </div>
      </header>

      <ControlPanel
        controls={controls}
        onToggleHeadroom={handleToggleHeadroom}
        togglingHeadroom={togglingHeadroom}
        onFireScripted={handleFireScripted}
        firingIndex={firingIndex}
      />

      <main className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-4 p-4 items-start">
        <div className="lg:col-span-4 flex flex-col gap-4">
          <FlowVisualizer trace={effectiveTrace} allowedProviders={allowedProviders} onpremOnly={onpremOnly} hops={hops} />
          <RequestList requests={requests} selectedId={selectedId} onSelect={setSelectedId} />
        </div>

        <div className="lg:col-span-4 flex flex-col gap-4">
          <DecisionInspector requestId={selectedId} detail={selectedDetail} loading={detailLoading} />
          <CompressionView detail={selectedDetail} />
        </div>

        <div className="lg:col-span-4">
          <CostDashboard stats={stats} requests={requests} />
        </div>
      </main>
    </div>
  );
}
