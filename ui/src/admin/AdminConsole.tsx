// Admin console shell: checks the session, shows login or the tabbed console.
import { useEffect, useState } from "react";
import { adminApi } from "../api";
import { AdminLogin } from "./AdminLogin";
import { ApplicationsPanel } from "./ApplicationsPanel";
import { UsagePanel } from "./UsagePanel";

type Auth = "checking" | "in" | "out";
type Tab = "applications" | "usage";

export function AdminConsole() {
  const [auth, setAuth] = useState<Auth>("checking");
  const [tab, setTab] = useState<Tab>("applications");

  useEffect(() => {
    adminApi.me().then(() => setAuth("in")).catch(() => setAuth("out"));
  }, []);

  if (auth === "checking") {
    return <div className="flex-1 flex items-center justify-center text-sm text-gray-500">Checking session…</div>;
  }
  if (auth === "out") {
    return <AdminLogin onLogin={() => setAuth("in")} />;
  }

  async function logout() {
    try { await adminApi.logout(); } catch { /* clear locally regardless */ }
    setAuth("out");
  }

  const tabBtn = (t: Tab) =>
    `px-3 py-1.5 rounded-md text-sm ${tab === t ? "bg-accent/15 text-accent border border-accent" : "text-gray-400 border border-transparent hover:text-gray-200"}`;

  return (
    <div className="flex-1 flex flex-col">
      <div className="border-b border-edge px-4 py-2 flex items-center gap-2">
        <button className={tabBtn("applications")} onClick={() => setTab("applications")}>Applications</button>
        <button className={tabBtn("usage")} onClick={() => setTab("usage")}>Usage</button>
        <button onClick={logout} className="ml-auto text-xs text-gray-500 hover:text-gray-300">Sign out</button>
      </div>
      <div className="flex-1 p-4">
        {tab === "applications" ? <ApplicationsPanel /> : <UsagePanel />}
      </div>
    </div>
  );
}
