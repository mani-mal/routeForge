// Console shell — the app's only top-level surface post-login. Owns
// navigation between the fleet landing page, the "manage applications"
// surface (create + list + per-app config, reusing the admin panel), the
// infrastructure section, and a single app's drill-down page. Nothing here
// talks to the backend directly; each child screen owns its own data.
//
// Navigation is the Switchyard "rail": a left column of lit "station" nodes
// threaded by a single vertical track (spec §12). Extend `STATIONS` (and the
// `View` union) to add sections — the "infra" station renders the real
// <InfrastructurePanel/> (Task B5).
import { useState } from "react";
import { FleetOverview } from "./FleetOverview";
import { AppPage } from "./AppPage";
import { ApplicationsPanel } from "../admin/ApplicationsPanel";
import { InfrastructurePanel } from "../admin/InfrastructurePanel";
import { CrossAppReport } from "../admin/CrossAppReport";

type View = "fleet" | "manage" | "app" | "infra" | "usage";

type StationKey = Exclude<View, "app">;

const STATIONS: { key: StationKey; label: string }[] = [
  { key: "fleet", label: "Overview" },
  { key: "manage", label: "Applications" },
  { key: "infra", label: "Infrastructure" },
  { key: "usage", label: "Cross-app usage" },
];

export function ConsoleShell() {
  const [view, setView] = useState<View>("fleet");
  const [appId, setAppId] = useState<string | null>(null);

  function openApp(id: string) {
    setAppId(id);
    setView("app");
  }

  function backToFleet() {
    setView("fleet");
    setAppId(null);
  }

  if (view === "app" && appId) {
    return <AppPage appId={appId} onBack={backToFleet} />;
  }

  return (
    <div className="flex-1 flex min-h-0">
      <nav aria-label="Console sections" className="w-14 sm:w-48 shrink-0 border-r border-edge py-4">
        <ol className="relative flex flex-col gap-1">
          {/* the rail's vertical track — a single line threading every station */}
          <div aria-hidden="true" className="absolute left-5 top-2 bottom-2 w-px bg-edge" />
          {STATIONS.map((s) => {
            const active = view === s.key;
            return (
              <li key={s.key}>
                <button
                  type="button"
                  onClick={() => setView(s.key)}
                  aria-current={active ? "page" : undefined}
                  aria-label={s.label}
                  title={s.label}
                  className={`relative z-10 flex w-full items-center gap-3 px-2 py-2.5 text-sm font-display transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset ${
                    active ? "text-accent" : "text-gray-500 hover:text-gray-300"
                  }`}
                >
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center" aria-hidden="true">
                    <span className={`h-2 w-2 rounded-full ${active ? "bg-accent" : "bg-gray-600"}`} />
                  </span>
                  <span className="hidden truncate sm:inline">{s.label}</span>
                </button>
              </li>
            );
          })}
        </ol>
      </nav>
      <div className="flex-1 min-w-0 overflow-auto p-4">
        {view === "manage" && <ApplicationsPanel />}
        {view === "infra" && <InfrastructurePanel />}
        {view === "usage" && <CrossAppReport />}
        {view === "fleet" && <FleetOverview onOpenApp={openApp} />}
      </div>
    </div>
  );
}
