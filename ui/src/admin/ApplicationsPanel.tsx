// Applications management: list, create (with one-time token), tokens, policy.
import { useEffect, useState } from "react";
import { adminApi } from "../api";
import type { AdminApp, AdminPolicy } from "../api";
import { Panel } from "../components/common";
import { PrivacyRulesEditor } from "./PrivacyRulesEditor";

const TIERS = ["cheap", "mid", "frontier"];
const PROVIDERS = ["onprem", "azure", "bedrock"];
const RESIDENCIES = ["any", "private_cloud_ok", "onprem_only"];

// A token shown exactly once, right after it's minted.
function TokenReveal({ token }: { token: string }) {
  return (
    <div className="rounded-md border border-accent/60 bg-accent/10 p-3 text-sm">
      <div className="text-accent font-medium mb-1">New token — copy it now, it won't be shown again</div>
      <code className="block break-all font-mono text-gray-100">{token}</code>
    </div>
  );
}

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

function multiToggle(list: string[] | null, value: string): string[] | null {
  const set = new Set(list ?? []);
  set.has(value) ? set.delete(value) : set.add(value);
  const arr = [...set];
  return arr.length ? arr : null; // empty => no restriction
}

function PolicyEditor({ app, onSaved }: { app: AdminApp; onSaved: () => void }) {
  const [p, setP] = useState<AdminPolicy>(
    app.policy ?? {
      allowed_tiers: null, allowed_providers: null, allowed_models: null,
      residency: null, max_spend_usd_per_day: null, max_rpm: null,
      compression_enabled: null, capture_io: null,
    });
  const [modelsText, setModelsText] = useState((p.allowed_models ?? []).join(", "));
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      const models = modelsText.split(",").map((m) => m.trim()).filter(Boolean);
      await adminApi.setPolicy(app.id, {
        ...p,
        allowed_models: models.length ? models : null,
      });
      onSaved();
    } finally {
      setBusy(false);
    }
  }

  const chip = (on: boolean) =>
    `px-2 py-0.5 rounded text-xs border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
      on ? "border-accent text-accent bg-accent/10" : "border-edge text-gray-500"
    }`;

  return (
    <div className="flex flex-col gap-2 text-sm">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-gray-500 w-20">tiers</span>
        {TIERS.map((t) => (
          <button key={t} className={chip((p.allowed_tiers ?? []).includes(t))}
            onClick={() => setP({ ...p, allowed_tiers: multiToggle(p.allowed_tiers, t) })}>{t}</button>
        ))}
        <span className="text-[10px] text-gray-600">none = all</span>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-gray-500 w-20">providers</span>
        {PROVIDERS.map((t) => (
          <button key={t} className={chip((p.allowed_providers ?? []).includes(t))}
            onClick={() => setP({ ...p, allowed_providers: multiToggle(p.allowed_providers, t) })}>{t}</button>
        ))}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-gray-500 w-20">residency</span>
        <select value={p.residency ?? ""} onChange={(e) => setP({ ...p, residency: e.target.value || null })}
          className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100">
          <option value="">(app default)</option>
          {RESIDENCIES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>
      <label className="flex items-center gap-2 flex-wrap text-xs text-gray-500">
        <span className="w-20">models</span>
        <input
          value={modelsText}
          onChange={(e) => setModelsText(e.target.value)}
          placeholder="local-8b, nvidia/… (empty = all)"
          className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 min-w-64 flex-1"
        />
        <span className="text-[10px] text-gray-600">comma-separated slugs; empty = all</span>
      </label>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-gray-500 w-20">Headroom</span>
        <select
          value={p.compression_enabled === null ? "" : p.compression_enabled ? "on" : "off"}
          onChange={(e) => setP({
            ...p,
            compression_enabled: e.target.value === "" ? null : e.target.value === "on",
          })}
          className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100">
          <option value="">(global default)</option>
          <option value="on">on</option>
          <option value="off">off</option>
        </select>
        <span className="text-[10px] text-gray-600">
          per-app context compression; safety bypasses still apply
        </span>
      </div>
      {(() => {
        const fullDisabled =
          (p.residency ?? app.default_residency) === "onprem_only" ||
          !app.langfuse_project_id;
        return (
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-gray-500 w-20">eval capture</span>
            <select
              value={p.capture_io ?? ""}
              onChange={(e) => setP({ ...p, capture_io: e.target.value || null })}
              className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100">
              <option value="">(global default)</option>
              <option value="off">off</option>
              <option value="redacted">redacted</option>
              <option value="full" disabled={fullDisabled && p.capture_io !== "full"}>
                full{fullDisabled ? " — needs own Langfuse + non-onprem" : ""}
              </option>
            </select>
            <span className="text-[10px] text-gray-600">
              raw (full) is stored only in this app's own residency-local project
            </span>
          </div>
        );
      })()}
      <div className="flex items-center gap-4 flex-wrap">
        <label className="flex items-center gap-2 text-xs text-gray-500">
          max rpm
          <input type="number" min={0} value={p.max_rpm ?? ""}
            onChange={(e) => setP({ ...p, max_rpm: e.target.value ? Number(e.target.value) : null })}
            className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 w-20" />
        </label>
        <label className="flex items-center gap-2 text-xs text-gray-500">
          budget $/day
          <input type="number" min={0} step="0.01" value={p.max_spend_usd_per_day ?? ""}
            onChange={(e) => setP({ ...p, max_spend_usd_per_day: e.target.value ? Number(e.target.value) : null })}
            className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 w-24" />
        </label>
        <button onClick={save} disabled={busy}
          className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1 text-xs disabled:opacity-50">
          {busy ? "saving…" : "Save policy"}
        </button>
      </div>
    </div>
  );
}

// Reusable per-application config UI: policy editor + token management.
// Fetches its own copy of the app (there is no single-get in adminApi, so it
// lists and finds by id) and re-fetches after every mutation. `onChanged` is
// an optional passthrough so a parent that keeps its own app list (e.g.
// ApplicationsPanel's left-hand list) can stay in sync too.
export function AppConfig({ appId, onChanged }: { appId: string; onChanged?: () => void }) {
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [loading, setLoading] = useState(true);
  const [newToken, setNewToken] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    try {
      const rows = await adminApi.apps();
      setApps(rows);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load().catch(() => {}); }, [appId]);

  const app = apps.find((a) => a.id === appId) ?? null;

  async function refresh() {
    await load();
    onChanged?.();
  }

  async function addToken() {
    const r = await adminApi.createToken(appId, "manual");
    setNewToken(r.token);
    await refresh();
  }
  async function revoke(id: string) {
    await adminApi.revokeToken(id);
    await refresh();
  }

  if (loading) return <p className="text-sm text-gray-500">Loading…</p>;
  if (!app) return <p className="text-sm text-frontier">Application not found.</p>;

  return (
    <div className="flex flex-col gap-4">
      <section>
        <div className="text-xs font-display uppercase tracking-wide text-gray-500 mb-1">Tokens</div>
        <ul className="flex flex-col gap-1">
          {app.tokens.map((t) => (
            <li key={t.id} className="flex items-center gap-2 text-sm">
              <code className="font-mono text-gray-300">{t.prefix}</code>
              <span className="text-gray-600">{t.name}</span>
              <span className={t.status === "active" ? "text-cheap text-xs" : "text-gray-600 text-xs"}>{t.status}</span>
              {t.status === "active" && (
                <button onClick={() => revoke(t.id)} className="ml-auto text-xs text-frontier hover:underline">revoke</button>
              )}
            </li>
          ))}
        </ul>
        <button onClick={addToken} className="mt-2 text-xs text-accent hover:underline">+ issue token</button>
        {newToken && <div className="mt-2"><TokenReveal token={newToken} /></div>}
      </section>

      <section>
        <div className="text-xs font-display uppercase tracking-wide text-gray-500 mb-1">Policy</div>
        <PolicyEditor app={app} onSaved={refresh} />
      </section>

      <section>
        <div className="text-xs font-display uppercase tracking-wide text-gray-500 mb-1">Privacy rules</div>
        <PrivacyRulesEditor appId={appId} />
      </section>
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
              <div className="text-sm font-semibold text-gray-100">{app.name} <span className="font-mono text-gray-500">({app.id})</span></div>
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
                  <span className="font-mono">{a.id}</span> · {a.default_residency} · {a.status}
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
