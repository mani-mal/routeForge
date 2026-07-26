// Applications management: list, create (with one-time token), tokens, policy.
import { useEffect, useState } from "react";
import { adminApi } from "../api";
import type { AdminApp, AdminPolicy } from "../api";
import { Panel } from "../components/common";

const TIERS = ["cheap", "mid", "frontier"];
const PROVIDERS = ["onprem", "azure", "bedrock"];
const RESIDENCIES = ["any", "private_cloud_ok", "onprem_only"];

// A token shown exactly once, right after it's minted.
function TokenReveal({ token }: { token: string }) {
  return (
    <div className="rounded-md border border-accent/60 bg-accent/10 p-3 text-sm">
      <div className="text-accent font-medium mb-1">New token — copy it now, it won't be shown again</div>
      <code className="block break-all text-gray-100">{token}</code>
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
    });
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      await adminApi.setPolicy(app.id, p);
      onSaved();
    } finally {
      setBusy(false);
    }
  }

  const chip = (on: boolean) =>
    `px-2 py-0.5 rounded text-xs border ${on ? "border-accent text-accent bg-accent/10" : "border-edge text-gray-500"}`;

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
      <div className="flex items-center gap-4 flex-wrap">
        <label className="flex items-center gap-2 text-xs text-gray-500">
          max rpm
          <input type="number" min={0} value={p.max_rpm ?? ""}
            onChange={(e) => setP({ ...p, max_rpm: e.target.value ? Number(e.target.value) : null })}
            className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100 w-20" />
        </label>
        <label className="flex items-center gap-2 text-xs text-gray-500">
          budget $/day
          <input type="number" min={0} step="0.01" value={p.max_spend_usd_per_day ?? ""}
            onChange={(e) => setP({ ...p, max_spend_usd_per_day: e.target.value ? Number(e.target.value) : null })}
            className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100 w-24" />
        </label>
        <button onClick={save} disabled={busy}
          className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1 text-xs disabled:opacity-50">
          {busy ? "saving…" : "Save policy"}
        </button>
      </div>
    </div>
  );
}

function AppDetail({ app, onChanged }: { app: AdminApp; onChanged: () => void }) {
  const [newToken, setNewToken] = useState<string | null>(null);

  async function addToken() {
    const r = await adminApi.createToken(app.id, "manual");
    setNewToken(r.token);
    onChanged();
  }
  async function revoke(id: string) {
    await adminApi.revokeToken(id);
    onChanged();
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold text-gray-100">{app.name} <span className="text-gray-500">({app.id})</span></div>
          <div className="text-xs text-gray-500">
            status {app.status} · default residency {app.default_residency}
            {app.langfuse_project_id ? ` · langfuse: ${app.langfuse_project_id}` : " · langfuse: shared"}
          </div>
        </div>
      </div>

      <section>
        <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">Tokens</div>
        <ul className="flex flex-col gap-1">
          {app.tokens.map((t) => (
            <li key={t.id} className="flex items-center gap-2 text-sm">
              <code className="text-gray-300">{t.prefix}</code>
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
        <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">Policy</div>
        <PolicyEditor app={app} onSaved={onChanged} />
      </section>
    </div>
  );
}

export function ApplicationsPanel() {
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [createdToken, setCreatedToken] = useState<string | null>(null);

  async function load() {
    const rows = await adminApi.apps();
    setApps(rows);
  }
  useEffect(() => { load().catch(() => {}); }, []);

  const current = apps.find((a) => a.id === selected) ?? null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <Panel title="Applications">
        <CreateApp onCreated={(t) => { setCreatedToken(t); load(); }} />
        {createdToken && <div className="mt-2"><TokenReveal token={createdToken} /></div>}
        <ul className="mt-3 flex flex-col gap-1">
          {apps.map((a) => (
            <li key={a.id}>
              <button onClick={() => setSelected(a.id)}
                className={`w-full text-left rounded-md border px-3 py-2 ${selected === a.id ? "border-accent bg-accent/5" : "border-edge hover:border-gray-600"}`}>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-100">{a.name}</span>
                  <span className="text-xs text-gray-500">{a.tokens.filter((t) => t.status === "active").length} tokens</span>
                </div>
                <div className="text-xs text-gray-500">{a.id} · {a.default_residency} · {a.status}</div>
              </button>
            </li>
          ))}
          {apps.length === 0 && <li className="text-sm text-gray-500">No applications yet.</li>}
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
