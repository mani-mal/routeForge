// Map each pool model to an Artificial Analysis slug. Suggestions come first;
// any other slug can be typed. The server validates against the latest snapshot.
import { useCallback, useEffect, useRef, useState } from "react";
import { adminApi } from "../../api";
import type { ExternalMap, ExternalMapRow } from "../../api";

const INPUT =
  "bg-ink border border-edge rounded-md px-2 py-1 text-sm text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";
const BTN =
  "rounded-md border border-edge px-2 py-1 text-sm text-gray-100 hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50";

function Row({ row, onSaved }: { row: ExternalMapRow; onSaved: () => void }) {
  const [slug, setSlug] = useState(row.ext_model_slug ?? "");
  const [saved, setSaved] = useState(row.ext_model_slug ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const listId = `aa-slugs-${row.model}`;

  async function save(value: string | null) {
    setBusy(true);
    setErr(null);
    try {
      await adminApi.externalSetMap(row.model, value);
      setSaved(value ?? "");
      setSlug(value ?? "");
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  }

  const dirty = slug.trim() !== saved;
  return (
    <li className="flex flex-col gap-1 border-b border-edge pb-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-[10rem] font-medium text-gray-50">{row.model}</span>
        <span className="text-xs text-gray-300">{row.placement === "onprem" ? "on-prem" : row.placement}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor={`aa-${row.model}`}>
          Artificial Analysis slug for {row.model}
        </label>
        <input
          id={`aa-${row.model}`}
          className={`${INPUT} w-64`}
          list={listId}
          value={slug}
          placeholder="not mapped"
          onChange={(e) => setSlug(e.target.value)}
        />
        <datalist id={listId}>
          {row.suggestions.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
        <button type="button" className={BTN} disabled={busy || !dirty || slug.trim() === ""} onClick={() => void save(slug.trim())}>
          Save
        </button>
        <button type="button" className={BTN} disabled={busy || saved === ""} onClick={() => void save(null)}>
          Clear
        </button>
      </div>
      {row.suggestions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-gray-300">
          Suggestions:
          {row.suggestions.map((s) => (
            <button key={s} type="button" className="rounded border border-edge px-1.5 py-0.5 text-gray-100 hover:border-accent" onClick={() => setSlug(s)}>
              {s}
            </button>
          ))}
        </div>
      )}
      {err && (
        <p role="alert" className="text-sm text-red-300">
          <span aria-hidden="true">✕ </span>
          {err}
        </p>
      )}
    </li>
  );
}

export function MappingDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [data, setData] = useState<ExternalMap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => opener?.focus?.();
  }, []);

  useEffect(() => {
    adminApi
      .externalMap()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load the mapping"));
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && dialogRef.current) {
        // keep focus inside the dialog
        const f = dialogRef.current.querySelectorAll<HTMLElement>("button:not([disabled]), input, a[href]");
        if (f.length === 0) return;
        const first = f[0];
        const last = f[f.length - 1];
        const active = document.activeElement;
        if (e.shiftKey && (active === first || !dialogRef.current.contains(active))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && (active === last || !dialogRef.current.contains(active))) {
          e.preventDefault();
          first.focus();
        }
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const saved = useCallback(() => onSaved(), [onSaved]);

  return (
    <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Map models to Artificial Analysis" className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-md border border-edge bg-panel p-4 shadow-xl">
        <div className="mb-3 flex items-start justify-between gap-2">
          <h2 className="font-display text-base font-semibold text-gray-50">Map models to Artificial Analysis</h2>
          <button ref={closeRef} type="button" className={BTN} onClick={onClose}>
            Close (Esc)
          </button>
        </div>
        <p className="mb-3 text-xs text-gray-300">
          Each save is checked against the latest snapshot. Unmapped models show no published figures.
        </p>
        <p className="mb-3 text-xs text-gray-300">
          Published data:{" "}
          <a href="https://artificialanalysis.ai/" target="_blank" rel="noopener noreferrer" className="text-accent underline">
            Artificial Analysis
          </a>
        </p>
        {error && (
          <p role="alert" className="text-sm text-red-300">
            {error}
          </p>
        )}
        {!data && !error && <p className="text-sm text-gray-300">Loading…</p>}
        {data && data.models.length === 0 && <p className="text-sm text-gray-300">No models in the pool.</p>}
        {data && (
          <ul className="flex flex-col gap-3">
            {data.models.map((m) => (
              <Row key={m.model} row={m} onSaved={saved} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
