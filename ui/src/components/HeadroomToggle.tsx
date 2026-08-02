// Global Headroom (compression) master switch for the console header.
//
// This is the GLOBAL default (serving.yaml context_optimization.enabled, mutated
// live via POST /api/controls — no restart). Per-application overrides set in an
// app's Settings tab (AppPolicy.compression_enabled) win over it; apps left at
// "(global default)" follow this switch. Safety bypasses (high-risk / restricted /
// below min-tokens) still apply on top regardless — see app/compression.
//
// The backend mutates app.state.co_cfg, the single source both the direct path and
// the per-hop agent path read, so the next request on either path picks up the flip.
import { useEffect, useState } from "react";
import { api } from "../api";

export function HeadroomToggle() {
  const [enabled, setEnabled] = useState<boolean | null>(null); // null = loading
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .controls()
      .then((c) => !cancelled && setEnabled(c.headroom_enabled))
      .catch(() => !cancelled && setErr(true));
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggle() {
    if (enabled === null || busy) return;
    const next = !enabled;
    setEnabled(next); // optimistic — the flip should feel instant on a projector
    setBusy(true);
    setErr(false);
    try {
      const c = await api.setControls(next);
      setEnabled(c.headroom_enabled); // reconcile with server truth
    } catch {
      setEnabled(!next); // revert
      setErr(true);
    } finally {
      setBusy(false);
    }
  }

  const on = enabled === true;
  const loading = enabled === null && !err;

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={loading || busy}
      aria-label="Toggle global Headroom compression"
      aria-pressed={on}
      title={
        err
          ? "Couldn't reach the compression control — click to retry"
          : "Global Headroom (compression) default. Applications set to on/off in their Settings override this; those left at “(global default)” follow it."
      }
      className="flex items-center gap-2 rounded-md border border-edge px-2.5 py-1.5 text-xs text-gray-400 hover:border-accent hover:text-gray-200 transition-colors disabled:opacity-60 disabled:cursor-wait focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <span className="uppercase tracking-wide text-[10px] text-gray-500">Headroom</span>
      <span
        className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors ${
          err ? "bg-frontier/50" : on ? "bg-cheap" : "bg-gray-600"
        }`}
        aria-hidden="true"
      >
        <span
          className={`inline-block h-3 w-3 rounded-full bg-white transition-transform ${
            on ? "translate-x-[15px]" : "translate-x-0.5"
          }`}
        />
      </span>
      <span className="tabular-nums w-6 text-left">{err ? "err" : loading ? "…" : on ? "on" : "off"}</span>
    </button>
  );
}
