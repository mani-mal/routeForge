// Model onboarding admin section. routeForge is a pure router — it deploys
// nothing. Operators stand up an OpenAI-compatible endpoint themselves (vLLM,
// Dynamo, whatever) and onboard it here at runtime: name, slug, tier,
// endpoint, price, and which applications may route to it. Backend:
// app/api/admin_routes.py — POST/GET /admin/infra/models, PUT/DELETE
// /admin/infra/models/{slug}. The PUT handler validates the full `OnboardIn`
// body (it re-probes the endpoint on every edit), so the enabled-toggle in
// the fleet table sends the row's full current field set with `enabled`
// flipped — not a partial `{enabled}` patch, which would 422.
//
// Disabled fleet rows reuse the "learned-once" dim-node metaphor from the
// junction inspector (RequestDrilldown's rejected-spur styling: opacity-60 on
// a bg-ink/40 row) — a disabled model dims exactly like an excluded candidate.
import { useEffect, useState } from "react";
import { onboardApi, adminApi } from "../api";
import type { OnboardedModel, OnboardIn, AdminApp } from "../api";
import { Panel, TierDot } from "../components/common";

const TIERS = ["cheap", "mid", "frontier"] as const;

// Shared toggle look (lifted from HeadroomToggle's switch) — used for each
// fleet row's enabled state.
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

// --- Health badge ------------------------------------------------------------

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

// --- Onboard form -------------------------------------------------------------

function OnboardForm({ apps, onOnboarded }: { apps: AdminApp[]; onOnboarded: () => void }) {
  const [displayName, setDisplayName] = useState("");
  const [slug, setSlug] = useState("");
  const [tier, setTier] = useState<string>("mid");
  const [endpoint, setEndpoint] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  const [priceInput, setPriceInput] = useState("");
  const [priceOutput, setPriceOutput] = useState("");
  const [quality, setQuality] = useState("");
  const [contextWindow, setContextWindow] = useState("");
  const [appIds, setAppIds] = useState<string[]>([]);
  const [notes, setNotes] = useState("");

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ probe_status: string; probe_error: string | null } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  function reset() {
    setDisplayName("");
    setSlug("");
    setTier("mid");
    setEndpoint("");
    setApiKeyEnv("");
    setPriceInput("");
    setPriceOutput("");
    setQuality("");
    setContextWindow("");
    setAppIds([]);
    setNotes("");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      const body: OnboardIn = {
        slug: slug.trim(),
        display_name: displayName.trim(),
        tier,
        endpoint: endpoint.trim(),
        api_key_env: apiKeyEnv.trim() ? apiKeyEnv.trim() : null,
        price_input: Number(priceInput),
        price_output: Number(priceOutput),
        quality: quality.trim() ? Number(quality) : null,
        context_window: contextWindow.trim() ? Number(contextWindow) : null,
        allowed_app_ids: appIds.length > 0 ? appIds : null,
        notes: notes.trim() ? notes.trim() : null,
      };
      const r = await onboardApi.create(body);
      setResult({ probe_status: r.probe_status, probe_error: r.probe_error });
      reset();
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
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            display name
            <input required value={displayName} onChange={(e) => setDisplayName(e.target.value)} className={inputCls} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            slug
            <input required value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="e.g. llama-3-70b-onprem" className={inputCls} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            tier
            <select value={tier} onChange={(e) => setTier(e.target.value)} className={inputCls}>
              {TIERS.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            endpoint
            <input required value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="http://host:port/v1" className={inputCls} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            api key env (optional)
            <input value={apiKeyEnv} onChange={(e) => setApiKeyEnv(e.target.value)} placeholder="(none)" className={inputCls} />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-xs text-gray-500">
              price / 1M input ($)
              <input required type="number" step="any" min={0} value={priceInput} onChange={(e) => setPriceInput(e.target.value)} className={inputCls} />
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-500">
              price / 1M output ($)
              <input required type="number" step="any" min={0} value={priceOutput} onChange={(e) => setPriceOutput(e.target.value)} className={inputCls} />
            </label>
          </div>
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            quality (optional)
            <input type="number" step="any" min={0} value={quality} onChange={(e) => setQuality(e.target.value)} placeholder="(none)" className={inputCls} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            context window (optional)
            <input type="number" step="1" min={0} value={contextWindow} onChange={(e) => setContextWindow(e.target.value)} placeholder="(none)" className={inputCls} />
          </label>
        </div>

        <label className="flex flex-col gap-1 text-xs text-gray-500">
          applications (none selected = all apps)
          <select
            multiple
            value={appIds}
            onChange={(e) => setAppIds(Array.from(e.target.selectedOptions).map((o) => o.value))}
            className={`${inputCls} h-24`}
          >
            {apps.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-gray-500">
          notes
          <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="(none)" className={inputCls} />
        </label>

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={busy}
            className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1.5 text-sm disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {busy ? "onboarding…" : "Onboard"}
          </button>
          {result && (
            <span className={`text-xs ${result.probe_status === "healthy" ? "text-cheap" : "text-alert"}`}>
              {result.probe_status === "healthy"
                ? "probe ok — enabled"
                : `probe failed — saved disabled: ${result.probe_error ?? "unreachable"}`}
            </span>
          )}
          {err && <span className="text-xs text-frontier">{err}</span>}
        </div>
      </form>
    </Panel>
  );
}

// --- Fleet table --------------------------------------------------------------

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

  async function toggleEnabled() {
    setBusy(true);
    setErr(null);
    try {
      // PUT validates the full OnboardIn body (it re-probes on every edit), so
      // we spread the row's current fields rather than sending a partial patch.
      const body: OnboardIn = {
        slug: model.slug,
        display_name: model.display_name,
        tier: model.tier,
        endpoint: model.endpoint,
        api_key_env: model.api_key_env,
        price_input: model.price_input,
        price_output: model.price_output,
        quality: model.quality,
        context_window: model.context_window,
        allowed_app_ids: model.allowed_app_ids,
        notes: model.notes,
      };
      // onChanged() below re-fetches the full list, so the row reflects the
      // PUT's actual persisted enabled/probe_status (post intent-AND-probe),
      // not an optimistic local flip.
      await onboardApi.update(model.slug, { ...body, enabled: !model.enabled });
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "update failed");
    } finally {
      setBusy(false);
    }
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
    // The dim-node metaphor (spec §12): a disabled row dims exactly like a
    // rejected/excluded candidate in the junction inspector — same idiom as
    // RequestDrilldown's rejected spurs (`bg-ink/40 ... opacity-60`), not just
    // faded text.
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
            onClick={remove}
            disabled={busy}
            className="rounded border border-edge text-gray-400 hover:text-alert hover:border-alert px-2 py-1 text-xs disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            delete
          </button>
          {err && <span className="text-xs text-frontier">{err}</span>}
        </div>
      </td>
    </tr>
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
