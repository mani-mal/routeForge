// Console shell — the app's only top-level surface post-login. Owns
// navigation between the fleet landing page, the "manage applications"
// surface (create + list + per-app config, reusing the admin panel), and a
// single app's drill-down page. Nothing here talks to the backend directly;
// each child screen owns its own data.
import { useState } from "react";
import { FleetOverview } from "./FleetOverview";
import { AppPage } from "./AppPage";
import { ApplicationsPanel } from "../admin/ApplicationsPanel";

type View = "fleet" | "manage" | "app";

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
    <div className="flex-1 flex flex-col">
      <div className="border-b border-edge px-4 py-3 flex items-center justify-between">
        <nav className="flex items-center gap-1 text-sm">
          <button
            onClick={() => setView("fleet")}
            className={`px-3 py-1 rounded-md ${view === "fleet" ? "bg-accent/15 text-accent" : "text-gray-400 hover:text-gray-200"}`}
          >
            Fleet
          </button>
          {view === "manage" && (
            <>
              <span className="text-gray-600">/</span>
              <span className="px-3 py-1 rounded-md bg-accent/15 text-accent">Manage applications</span>
            </>
          )}
        </nav>
        <button
          onClick={() => setView(view === "manage" ? "fleet" : "manage")}
          className="text-sm text-gray-400 hover:text-gray-200 border border-edge rounded-md px-3 py-1.5"
        >
          {view === "manage" ? "← Fleet" : "＋ New application"}
        </button>
      </div>
      <div className="flex-1 p-4">
        {view === "manage" ? <ApplicationsPanel /> : <FleetOverview onOpenApp={openApp} />}
      </div>
    </div>
  );
}
