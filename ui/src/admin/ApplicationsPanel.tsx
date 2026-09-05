// Applications management: list, create (with one-time token), tokens, policy.
//
// The per-application Settings tab itself lives in AppSettings.tsx (six
// numbered collapsible steps); `AppConfig` is re-exported from here so
// console/AppPage.tsx keeps its existing import.
import { useEffect, useState } from "react";
import { adminApi } from "../api";
import type { AdminApp } from "../api";
import { Panel } from "../components/common";
import { AppConfig, RESIDENCIES } from "./AppSettings";
import { CopyableId, TokenReveal } from "./clipboard";

export { AppConfig };

function CreateApp({ onCreated }: { onCreated: (token: string) => void }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [residency, setResidency] = useState("any");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    setErr(null);
    try {
      const r = await adminApi.createApp({ id, name, default_residency: residency });
      onCreated(r.token);
      setId("");
      setName("");
    } catch {
      setErr("Could not create — is the id already taken?");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-end gap-2">
      <label className="flex flex-col gap-1 text-xs text-gray-400">
        id
        <input value={id} onChange={(e) => setId(e.target.value)} placeholder="my-app"
          className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100 w-32" />
      </label>
      <label className="flex flex-col gap-1 text-xs text-gray-400">
        name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="My App"
          className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100 w-40" />
      </label>
      <label className="flex flex-col gap-1 text-xs text-gray-400">
        residency
        <select value={residency} onChange={(e) => setResidency(e.target.value)}
          className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100">
          {RESIDENCIES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </label>
      <button onClick={create} disabled={!id || !name || busy}
        className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1.5 text-sm disabled:opacity-50">
        {busy ? "creating…" : "Create application"}
      </button>
      {err && <span className="text-xs text-frontier">{err}</span>}
    </div>
  );
}

function AppDetail({ app, onChanged }: { app: AdminApp; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(app.name);
  const [owner, setOwner] = useState(app.owner_email ?? "");
  const [residency, setResidency] = useState(app.default_residency);
  const [status, setStatus] = useState(app.status);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Re-seed fields whenever a different app is selected.
  useEffect(() => {
    setEditing(false);
    setName(app.name);
    setOwner(app.owner_email ?? "");
    setResidency(app.default_residency);
    setStatus(app.status);
    setErr(null);
  }, [app.id]);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      await adminApi.updateApp(app.id, {
        name,
        owner_email: owner,
        default_residency: residency,
        status,
      });
      setEditing(false);
      onChanged();
    } catch {
      setErr("Could not save changes.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-2">
        {editing ? (
          <div className="flex flex-col gap-2 flex-1">
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              name
              <input value={name} onChange={(e) => setName(e.target.value)}
                className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100 w-56" />
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              owner email
              <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="(none)"
                className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100 w-56" />
            </label>
            <div className="flex items-end gap-2">
              <label className="flex flex-col gap-1 text-xs text-gray-400">
                default residency
                <select value={residency} onChange={(e) => setResidency(e.target.value)}
                  className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100">
                  {RESIDENCIES.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs text-gray-400">
                status
                <select value={status} onChange={(e) => setStatus(e.target.value)}
                  className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100">
                  <option value="active">active</option>
                  <option value="archived">archived</option>
                </select>
              </label>
            </div>
            <div className="flex items-center gap-2">
              <button onClick={save} disabled={!name || busy}
                className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1 text-xs disabled:opacity-50">
                {busy ? "saving…" : "Save"}
              </button>
              <button
                onClick={() => {
                  setName(app.name);
                  setOwner(app.owner_email ?? "");
                  setResidency(app.default_residency);
                  setStatus(app.status);
                  setErr(null);
                  setEditing(false);
                }}
                disabled={busy}
                className="text-xs text-gray-500 hover:underline">Cancel</button>
              {err && <span className="text-xs text-frontier">{err}</span>}
            </div>
          </div>
        ) : (
          <>
            <div>
              <div className="flex flex-col gap-1">
              <div className="text-sm font-semibold text-gray-100">{app.name}</div>
              <CopyableId value={app.id} />
            </div>
              <div className="text-xs text-gray-500">
                status {app.status} · default residency {app.default_residency}
                {app.langfuse_project_id ? ` · langfuse: ${app.langfuse_project_id}` : " · langfuse: shared"}
              </div>
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0">
              <button onClick={() => setEditing(true)}
                className="text-xs text-accent hover:underline">Edit</button>
              {app.status === "archived" ? (
                <button
                  onClick={async () => { await adminApi.updateApp(app.id, { status: "active" }); onChanged(); }}
                  className="text-xs text-cheap hover:underline">Restore</button>
              ) : (
                <button
                  onClick={async () => {
                    if (!window.confirm(`Archive ${app.name}? Its tokens stop working; you can restore it later.`)) return;
                    await adminApi.updateApp(app.id, { status: "archived" });
                    onChanged();
                  }}
                  className="text-xs text-frontier hover:underline">Archive</button>
              )}
            </div>
          </>
        )}
      </div>

      <AppConfig appId={app.id} onChanged={onChanged} />
    </div>
  );
}

export function ApplicationsPanel() {
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);

  async function load() {
    const rows = await adminApi.apps();
    setApps(rows);
  }
  useEffect(() => { load().catch(() => {}); }, []);

  const visible = apps.filter((a) => showArchived || a.status !== "archived");
  // If the selected app is now hidden, drop the selection so the detail pane
  // never shows an app that isn't in the list.
  useEffect(() => {
    if (selected && !visible.some((a) => a.id === selected)) setSelected(null);
  }, [selected, showArchived, apps]);
  const current = apps.find((a) => a.id === selected) ?? null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <Panel title="Applications">
        <CreateApp onCreated={(t) => { setCreatedToken(t); load(); }} />
        {createdToken && <div className="mt-2"><TokenReveal token={createdToken} /></div>}
        <label className="mt-3 flex items-center gap-2 text-xs text-gray-500">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          Show archived
        </label>
        <ul className="mt-2 flex flex-col gap-1">
          {visible.map((a) => (
            <li key={a.id}>
              <button onClick={() => setSelected(a.id)}
                className={`w-full text-left rounded-md border px-3 py-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${selected === a.id ? "border-accent bg-accent/5" : "border-edge hover:border-accent"} ${a.status === "archived" ? "opacity-50" : ""}`}>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-100">{a.name}{a.status === "archived" && <span className="ml-1 text-[10px] uppercase tracking-wide text-gray-500">archived</span>}</span>
                  <span className="text-xs font-mono text-gray-500">{a.tokens.filter((t) => t.status === "active").length} tokens</span>
                </div>
                <div className="text-xs text-gray-500">
                  <span className="text-gray-500">id</span> <span className="font-mono text-gray-300">{a.id}</span> · {a.default_residency} · {a.status}
                </div>
              </button>
            </li>
          ))}
          {visible.length === 0 && <li className="text-sm text-gray-500">No applications{showArchived ? "" : " (archived hidden)"}.</li>}
        </ul>
      </Panel>

      <Panel title="Application detail">
        {current ? (
          <AppDetail app={current} onChanged={load} />
        ) : (
          <p className="text-sm text-gray-500">Select an application to manage its tokens and policy.</p>
        )}
      </Panel>
    </div>
  );
}
