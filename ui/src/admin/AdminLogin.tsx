// Admin login — the only unauthenticated admin screen.
import { useState } from "react";
import { adminApi } from "../api";

export function AdminLogin({ onLogin }: { onLogin: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await adminApi.login(email, password);
      onLogin();
    } catch {
      setError("Invalid email or password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 flex items-center justify-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm bg-panel border border-edge rounded-lg p-6 flex flex-col gap-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-100">Admin sign in</h2>
          <p className="text-xs text-gray-500 mt-1">routeForge operator console</p>
        </div>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-gray-400">Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
            className="rounded-md bg-ink border border-edge px-3 py-2 text-gray-100 focus:border-accent outline-none"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-gray-400">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="rounded-md bg-ink border border-edge px-3 py-2 text-gray-100 focus:border-accent outline-none"
          />
        </label>
        {error && <p className="text-xs text-frontier">{error}</p>}
        <button
          type="submit"
          disabled={busy || !email || !password}
          className="rounded-md bg-accent/20 border border-accent text-accent font-medium py-2 disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
