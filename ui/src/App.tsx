// App shell — header (brand, connection dot, theme toggle) plus an admin
// auth gate guarding the single console surface. There is no more Live/Admin
// toggle: the per-application console (ConsoleShell) is the whole product.
import { useEffect, useState } from "react";
import { adminApi, subscribeStream } from "./api";
import { AdminLogin } from "./admin/AdminLogin";
import { ConsoleShell } from "./console/ConsoleShell";
import { RouteForgeLogo, CdwMark, ThemeToggle } from "./components/BrandLogo";
import { HeadroomToggle } from "./components/HeadroomToggle";
import { useTheme } from "./theme";

type Auth = "checking" | "in" | "out";

export default function App() {
  const [auth, setAuth] = useState<Auth>("checking");
  const [connected, setConnected] = useState(false);
  const [theme, toggleTheme] = useTheme();

  // Admin session check.
  useEffect(() => {
    let cancelled = false;
    adminApi
      .me()
      .then(() => !cancelled && setAuth("in"))
      .catch(() => !cancelled && setAuth("out"));
    return () => {
      cancelled = true;
    };
  }, []);

  // Lightweight status-only stream subscription, just to keep the header's
  // connection dot truthful.
  useEffect(() => {
    const unsubscribe = subscribeStream(
      () => {},
      (isConnected) => setConnected(isConnected),
    );
    return () => {
      unsubscribe();
      setConnected(false);
    };
  }, []);

  async function handleLogout() {
    try {
      await adminApi.logout();
    } catch {
      // clear locally regardless
    }
    setAuth("out");
  }

  return (
    <div className="min-h-screen bg-ink text-gray-100 flex flex-col">
      <header className="border-b border-edge px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <RouteForgeLogo />
        </div>
        <div className="flex items-center gap-4 text-sm text-gray-400">
          <span className="flex items-center gap-2">
            <span className={`h-2.5 w-2.5 rounded-full ${connected ? "bg-cheap" : "bg-gray-600"}`} />
            {connected ? "live" : "disconnected"}
          </span>
          {auth === "in" && <HeadroomToggle />}
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          {auth === "in" && (
            <button onClick={handleLogout} className="text-xs text-gray-500 hover:text-gray-300">
              Sign out
            </button>
          )}
          <span className="flex items-center gap-2 pl-4 border-l border-edge">
            <span className="text-[10px] uppercase tracking-wide text-gray-500">powered by</span>
            <CdwMark className="h-5 w-auto" />
          </span>
        </div>
      </header>

      {auth === "checking" && (
        <div className="flex-1 flex items-center justify-center text-sm text-gray-500">Checking session…</div>
      )}
      {auth === "out" && <AdminLogin onLogin={() => setAuth("in")} />}
      {auth === "in" && <ConsoleShell />}
    </div>
  );
}
