import { useEffect, useState } from "react";
import { onboardApi, adminApi } from "../api";
import type { OnboardedModel, OnboardIn, AdminApp } from "../api";
import { Panel, TierDot } from "../components/common";

const TIERS = ["cheap", "mid", "frontier"] as const;

function Toggle({ on, onToggle, label }: { on: boolean; onToggle: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={on}
      aria-label={label}
      title={label}
      className="relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <span
        aria-hidden="true"
        className={`absolute inset-0 rounded-full transition-colors ${on ? "bg-cheap" : "bg-gray-600"}`}
      />
      <span
        aria-hidden="true"
        className={`relative inline-block h-3 w-3 rounded-full bg-white transition-transform ${
          on ? "translate-x-[15px]" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

const inputCls =
  "rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";

function HealthBadge({ status, error }: { status: string; error: string | null }) {
  const cls =
    status === "healthy" ? "text-cheap" : status === "unreachable" ? "text-alert" : "text-gray-500";
  const mark = status === "healthy" ? "●" : status === "unreachable" ? "●" : "○";
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap font-mono text-xs ${cls}`} title={error ?? undefined}>
      <span aria-hidden="true">{mark}</span>
      {status}
    </span>
  );
}

// The onboard/edit field set, held as strings (what the inputs actually carry)
// so one control block serves both forms. Onboard (POST) and edit (PUT) take
// the same full `OnboardIn` body server-side, so they take the same fields.
type ModelFormFields = {
  slug: string;
  displayName: string;
  tier: string;
  endpoint: string;
  apiKeyEnv: string;
  priceInput: string;
  priceOutput: string;
  quality: string;
  contextWindow: string;
  appIds: string[];
  notes: string;
};

const EMPTY_FIELDS: ModelFormFields = {
  slug: "", displayName: "", tier: "mid", endpoint: "", apiKeyEnv: "",
  priceInput: "", priceOutput: "", quality: "", contextWindow: "",
  appIds: [], notes: "",
};

// Seed the edit form from the row the server already stored.
function fieldsOf(m: OnboardedModel): ModelFormFields {
  return {
    slug: m.slug,
    displayName: m.display_name,
    tier: m.tier,
    endpoint: m.endpoint,
    apiKeyEnv: m.api_key_env ?? "",
    priceInput: String(m.price_input),
    priceOutput: String(m.price_output),
    quality: m.quality === null ? "" : String(m.quality),
    contextWindow: m.context_window === null ? "" : String(m.context_window),
    appIds: m.allowed_app_ids ?? [],
    notes: m.notes ?? "",
  };
}

// One body shape for POST and PUT — the server re-validates and re-probes the
// whole body on either. `enabled` is explicit admin intent; the server ANDs it
// with the probe result, so an unreachable model is always saved disabled.
function toBody(f: ModelFormFields, enabled: boolean): OnboardIn {
  return {
    slug: f.slug.trim(),
    display_name: f.displayName.trim(),
    tier: f.tier,
    endpoint: f.endpoint.trim(),
    api_key_env: f.apiKeyEnv.trim() ? f.apiKeyEnv.trim() : null,
    price_input: Number(f.priceInput),
    price_output: Number(f.priceOutput),
    quality: f.quality.trim() ? Number(f.quality) : null,
    context_window: f.contextWindow.trim() ? Number(f.contextWindow) : null,
    allowed_app_ids: f.appIds.length > 0 ? f.appIds : null,
    notes: f.notes.trim() ? f.notes.trim() : null,
    enabled,
  };
}

// The shared control block. `lockSlug` renders slug read-only: the PUT is keyed
// by the path slug, so an edit can never rename a model — that is a delete +
// re-onboard (B21).
function ModelFields({
  fields,
  onChange,
  apps,
  lockSlug = false,
}: {
  fields: ModelFormFields;
  onChange: (f: ModelFormFields) => void;
  apps: AdminApp[];
  lockSlug?: boolean;
}) {
  const set = (patch: Partial<ModelFormFields>) => onChange({ ...fields, ...patch });
  return (
    <>
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          display name
          <input required value={fields.displayName} onChange={(e) => set({ displayName: e.target.value })} className={inputCls} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          slug{lockSlug ? " (fixed — delete & re-onboard to change)" : ""}
          <input
            required
            readOnly={lockSlug}
            value={fields.slug}
            onChange={(e) => set({ slug: e.target.value })}
            placeholder="e.g. llama-3-70b-onprem"
            title={lockSlug ? "The slug is the model's identity and the key the endpoint is probed for — to change it, delete the model and onboard it again." : undefined}
            className={`${inputCls}${lockSlug ? " text-gray-500 cursor-not-allowed" : ""}`}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          tier
          <select value={fields.tier} onChange={(e) => set({ tier: e.target.value })} className={inputCls}>
            {TIERS.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          endpoint
          <input required value={fields.endpoint} onChange={(e) => set({ endpoint: e.target.value })} placeholder="http://host:port/v1" className={inputCls} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          api key env (optional)
          <input value={fields.apiKeyEnv} onChange={(e) => set({ apiKeyEnv: e.target.value })} placeholder="(none)" className={inputCls} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            price / 1M input ($)
            <input required type="number" step="any" min={0} value={fields.priceInput} onChange={(e) => set({ priceInput: e.target.value })} className={inputCls} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            price / 1M output ($)
            <input required type="number" step="any" min={0} value={fields.priceOutput} onChange={(e) => set({ priceOutput: e.target.value })} className={inputCls} />
          </label>
        </div>
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          quality (optional)
          <input type="number" step="any" min={0} value={fields.quality} onChange={(e) => set({ quality: e.target.value })} placeholder="(none)" className={inputCls} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          context window (optional)
          <input type="number" step="1" min={0} value={fields.contextWindow} onChange={(e) => set({ contextWindow: e.target.value })} placeholder="(none)" className={inputCls} />
        </label>
      </div>

      <label className="flex flex-col gap-1 text-xs text-gray-500">
        applications (none selected = all apps)
        <select
          multiple
          value={fields.appIds}
          onChange={(e) => set({ appIds: Array.from(e.target.selectedOptions).map((o) => o.value) })}
          className={`${inputCls} h-24`}
        >
          {apps.map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-xs text-gray-500">
        notes
        <input value={fields.notes} onChange={(e) => set({ notes: e.target.value })} placeholder="(none)" className={inputCls} />
      </label>
    </>
  );
}

// What the POST/PUT ack tells us: the probe re-ran, and `enabled` is the
// intent-AND-probe result. Both forms report it the same way.
function ProbeResult({ status, error }: { status: string; error: string | null }) {
  return (
    <span className={`text-xs ${status === "healthy" ? "text-cheap" : "text-alert"}`}>
      {status === "healthy"
        ? "probe ok — enabled"
        : `probe failed — saved disabled: ${error ?? "unreachable"}`}
    </span>
  );
}

function OnboardForm({ apps, onOnboarded }: { apps: AdminApp[]; onOnboarded: () => void }) {
  const [fields, setFields] = useState<ModelFormFields>(EMPTY_FIELDS);

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ probe_status: string; probe_error: string | null } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      const r = await onboardApi.create(toBody(fields, true));
      setResult({ probe_status: r.probe_status, probe_error: r.probe_error });
      setFields(EMPTY_FIELDS);
      onOnboarded();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "onboard failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Onboard a model">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <ModelFields fields={fields} onChange={setFields} apps={apps} />

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={busy}
            className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1.5 text-sm disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {busy ? "onboarding…" : "Onboard"}
          </button>
          {result && <ProbeResult status={result.probe_status} error={result.probe_error} />}
          {err && <span className="text-xs text-frontier">{err}</span>}
        </div>
      </form>
    </Panel>
  );
}

function ModelRow({
  model,
  apps,
  onChanged,
}: {
  model: OnboardedModel;
  apps: AdminApp[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [fields, setFields] = useState<ModelFormFields>(() => fieldsOf(model));
  const [result, setResult] = useState<{ probe_status: string; probe_error: string | null } | null>(null);

  // Re-seed from the server row whenever it changes underneath us — the panel
  // refetches after every mutation — but never while the operator is typing.
  useEffect(() => {
    if (!editing) setFields(fieldsOf(model));
  }, [model, editing]);

  async function toggleEnabled() {
    setBusy(true);
    setErr(null);
    try {
      await onboardApi.update(model.slug, toBody(fieldsOf(model), !model.enabled));
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "update failed");
    } finally {
      setBusy(false);
    }
  }

  // B21: edit an onboarded model in place. Same PUT the toggle uses, with the
  // edited body instead of a flipped `enabled` — enablement stays the Toggle's
  // job, so an edit carries the model's current state through. The PUT re-probes
  // the (possibly new) endpoint for this slug, so the ack's probe outcome is
  // reported and the panel refetch below re-renders the health badge from it.
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      const r = await onboardApi.update(model.slug, toBody(fields, model.enabled));
      setResult({ probe_status: r.probe_status, probe_error: r.probe_error });
      setEditing(false);
      onChanged();
    } catch (e) {
      // 404/409/422 from the admin clamps (bad tier, non-http endpoint,
      // negative price, unknown application) — say so instead of silently
      // leaving the form as it was.
      setErr(e instanceof Error ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  }

  function cancel() {
    setEditing(false); // the effect above re-seeds the fields from the row
    setErr(null);
  }

  async function remove() {
    if (!window.confirm(`Remove ${model.display_name} (${model.slug})?`)) return;
    setBusy(true);
    setErr(null);
    try {
      await onboardApi.remove(model.slug);
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "delete failed");
    } finally {
      setBusy(false);
    }
  }

  const appNames =
    model.allowed_app_ids && model.allowed_app_ids.length > 0
      ? model.allowed_app_ids
          .map((id) => apps.find((a) => a.id === id)?.name ?? id)
          .join(", ")
      : "all";

  return (
    <>
      <tr
        className={`border-b border-edge/60 last:border-0 transition-colors ${
          model.enabled ? "" : "bg-ink/40 opacity-60"
        }`}
      >
        <td className="py-2 pr-3 align-top">
          <div className="text-sm text-gray-100">{model.display_name}</div>
          <div className="font-mono text-xs text-gray-500">{model.slug}</div>
        </td>
        <td className="py-2 pr-3 align-top">
          <span className="flex items-center gap-1.5">
            <TierDot tier={model.tier} />
            <span className="text-xs text-gray-400">{model.tier}</span>
          </span>
        </td>
        <td className="py-2 pr-3 align-top">
          <span className="font-mono text-xs text-gray-400 break-all">{model.endpoint}</span>
        </td>
        <td className="py-2 pr-3 align-top">
          <span className="text-xs text-gray-400">{appNames}</span>
        </td>
        <td className="py-2 pr-3 align-top">
          <Toggle on={model.enabled} onToggle={toggleEnabled} label={`${model.display_name} enabled`} />
        </td>
        <td className="py-2 pr-3 align-top">
          <HealthBadge status={model.probe_status} error={model.probe_error} />
        </td>
        <td className="py-2 align-top">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => (editing ? cancel() : setEditing(true))}
              disabled={busy}
              aria-expanded={editing}
              className="rounded border border-edge text-gray-400 hover:text-accent hover:border-accent px-2 py-1 text-xs disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {editing ? "close" : "edit"}
            </button>
            <button
              type="button"
              onClick={remove}
              disabled={busy}
              className="rounded border border-edge text-gray-400 hover:text-alert hover:border-alert px-2 py-1 text-xs disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              delete
            </button>
            {!editing && result && <ProbeResult status={result.probe_status} error={result.probe_error} />}
            {!editing && err && <span className="text-xs text-frontier">{err}</span>}
          </div>
        </td>
      </tr>
      {editing && (
        <tr className="border-b border-edge/60 last:border-0">
          <td colSpan={7} className="pb-3">
            <form onSubmit={save} className="flex flex-col gap-3 rounded-md border border-edge bg-ink/40 p-3">
              <ModelFields fields={fields} onChange={setFields} apps={apps} lockSlug />
              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={busy}
                  className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1 text-xs disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {busy ? "saving…" : "Save"}
                </button>
                <button
                  type="button"
                  onClick={cancel}
                  disabled={busy}
                  className="text-xs text-gray-500 hover:underline"
                >
                  Cancel
                </button>
                <span className="text-[10px] text-gray-600">
                  saving re-probes the endpoint for this slug; an unreachable model is saved disabled
                </span>
                {err && <span className="text-xs text-frontier">{err}</span>}
              </div>
            </form>
          </td>
        </tr>
      )}
    </>
  );
}

function FleetTable({ models, apps, onChanged }: { models: OnboardedModel[]; apps: AdminApp[]; onChanged: () => void }) {
  return (
    <Panel title="On-prem fleet">
      {models.length === 0 ? (
        <p className="text-sm text-gray-500">No models onboarded — add one above.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] border-collapse text-left">
            <thead>
              <tr className="border-b border-edge text-[10px] uppercase tracking-wide text-gray-500">
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Model</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Tier</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Endpoint</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Apps</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Enabled</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Health</th>
                <th scope="col" className="py-2 font-display font-semibold">&nbsp;</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <ModelRow key={m.slug} model={m} apps={apps} onChanged={onChanged} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

export function InfrastructurePanel() {
  const [models, setModels] = useState<OnboardedModel[] | null>(null);
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [err, setErr] = useState(false);

  async function load() {
    try {
      const [rows, appRows] = await Promise.all([onboardApi.list(), adminApi.apps()]);
      setModels(rows);
      setApps(appRows);
      setErr(false);
    } catch {
      setErr(true);
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <OnboardForm apps={apps} onOnboarded={load} />
      {err && (
        <Panel title="On-prem fleet">
          <p className="text-sm text-frontier">Could not load the on-prem fleet.</p>
        </Panel>
      )}
      {!err && models === null && (
        <Panel title="On-prem fleet">
          <p className="text-sm text-gray-500">Loading…</p>
        </Panel>
      )}
      {!err && models !== null && <FleetTable models={models} apps={apps} onChanged={load} />}
    </div>
  );
}
