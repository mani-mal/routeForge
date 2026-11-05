// Artificial Analysis controls: freshness stamp, Refresh (admin-triggered,
// never automatic), Import file (for air-gapped sites) and the mapping dialog.
import { useEffect, useRef, useState } from "react";
import { adminApi } from "../../api";
import type { ExternalSnapshots, PublishedSnapshotInfo } from "../../api";
import { stamp } from "./format";
import { MappingDialog } from "./MappingDialog";

const BTN =
  "rounded-md border border-edge px-3 py-1.5 text-sm text-gray-100 hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50";

export function ExternalControls({
  snapshot,
  onChanged,
}: {
  snapshot: PublishedSnapshotInfo | null | undefined;
  onChanged: () => void;
}) {
  const [info, setInfo] = useState<ExternalSnapshots | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [mapOpen, setMapOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    adminApi
      .externalSnapshots()
      .then((s) => {
        if (!cancelled) setInfo(s);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [snapshot?.snapshot_id]);

  async function run(label: string, fn: () => Promise<{ snapshot_id: number; row_count: number }>) {
    setBusy(true);
    setMsg(null);
    try {
      const r = await fn();
      setMsg({ ok: true, text: `${label} stored snapshot #${r.snapshot_id} (${r.row_count} models).` });
      onChanged();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : `${label} failed` });
    } finally {
      setBusy(false);
    }
  }

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) void run("Import", () => adminApi.externalImport(f));
  }

  const disabledReason = info && !info.fetch_enabled ? (info.fetch_disabled_reason ?? "Refresh is disabled") : null;

  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-gray-100">
        {snapshot
          ? `Artificial Analysis snapshot: ${stamp(snapshot.retrieved_at)}${
              snapshot.dated_at_upload ? " (date of upload, not of fetch)" : ""
            } (${snapshot.index_version ? `index ${snapshot.index_version}` : "index version not recorded"})`
          : "Artificial Analysis snapshot: none imported yet"}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className={BTN}
          disabled={busy || info === null || disabledReason !== null}
          onClick={() => void run("Refresh", () => adminApi.externalRefresh())}
        >
          Refresh
        </button>
        <button type="button" className={BTN} disabled={busy} onClick={() => fileRef.current?.click()}>
          Import file
        </button>
        <input ref={fileRef} type="file" accept="application/json" className="sr-only" aria-label="Import Artificial Analysis JSON file" onChange={onFile} />
        <button type="button" className="text-sm text-accent underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent" onClick={() => setMapOpen(true)}>
          Map models
        </button>
        {busy && <span className="text-xs text-gray-300">Working…</span>}
      </div>
      {disabledReason && <p className="text-xs text-gray-300">Refresh unavailable: {disabledReason}</p>}
      {msg && (
        <p role={msg.ok ? "status" : "alert"} className={`text-sm ${msg.ok ? "text-gray-100" : "text-red-300"}`}>
          <span aria-hidden="true">{msg.ok ? "✓ " : "✕ "}</span>
          {msg.text}
        </p>
      )}
      {mapOpen && (
        <MappingDialog
          onClose={() => setMapOpen(false)}
          onSaved={onChanged}
        />
      )}
    </div>
  );
}
