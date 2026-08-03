// Infrastructure admin section — Task B5. Two panels in the Switchyard
// language: the Cluster card (Run:ai declaration + default engine, backed by
// GET/PUT /admin/infra) and the On-prem fleet table (per-model engine/enabled/
// capacity, backed by GET /admin/infra/models + PUT /admin/infra/models/{slug}).
//
// This layer only describes and consumes state the backend already owns — no
// deploy, no Run:ai/K8s call, no probe (CLAUDE.md Rule 4). `runai_licensed` is
// a DECLARED admin fact, not a verified one (spec §8): routeForge never checks
// it against the cluster, so the UI must never imply otherwise. When the
// toggle is on we label it "Run:ai — declared licensed", not a bare "licensed"
// badge — that distinction is the entire point of this control.
//
// Disabled fleet rows reuse the "learned-once" dim-node metaphor from the
// junction inspector (RequestDrilldown's rejected-spur styling: opacity-60 on
// a bg-ink/40 row) — a disabled model dims exactly like an excluded candidate.
import { useEffect, useState } from "react";
import { infraApi } from "../api";
import type { InfraModel, InfraSettings } from "../api";
import { Panel, TierDot } from "../components/common";

const ENGINES: { value: string; label: string }[] = [
  { value: "vllm", label: "vLLM" },
  { value: "trtllm", label: "TensorRT-LLM" },
];

function EngineSelect({
  value,
  onChange,
  className = "",
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={ariaLabel}
      className={`rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${className}`}
    >
      {ENGINES.map((e) => (
        <option key={e.value} value={e.value}>{e.label}</option>
      ))}
    </select>
  );
}

// Shared toggle look (lifted from HeadroomToggle's switch) — used for both the
// cluster card's Run:ai-licensed control and each fleet row's enabled state.
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

// --- Cluster card -----------------------------------------------------------

function ClusterCard() {
  const [settings, setSettings] = useState<InfraSettings | null>(null);
  const [runaiLicensed, setRunaiLicensed] = useState(false);
  const [runaiProject, setRunaiProject] = useState("");
  const [defaultEngine, setDefaultEngine] = useState("vllm");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [loadErr, setLoadErr] = useState(false);
  const [saveErr, setSaveErr] = useState(false);

  async function load() {
    const s = await infraApi.get();
    setSettings(s);
    setRunaiLicensed(s.runai_licensed);
    setRunaiProject(s.runai_project ?? "");
    setDefaultEngine(s.default_engine);
  }

  useEffect(() => {
    load().catch(() => setLoadErr(true));
  }, []);

  async function save() {
    setBusy(true);
    setSaveErr(false);
    setSaved(false);
    try {
      await infraApi.put({
        runai_licensed: runaiLicensed,
        runai_project: runaiProject.trim() ? runaiProject.trim() : null,
        default_engine: defaultEngine,
      });
      await load();
      setSaved(true);
    } catch {
      setSaveErr(true);
    } finally {
      setBusy(false);
    }
  }

  if (loadErr) {
    return (
      <Panel title="Cluster">
        <p className="text-sm text-frontier">Could not load infrastructure settings.</p>
      </Panel>
    );
  }
  if (!settings) {
    return (
      <Panel title="Cluster">
        <p className="text-sm text-gray-500">Loading…</p>
      </Panel>
    );
  }

  return (
    <Panel title="Cluster">
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-3 text-sm">
          <span className="w-36 shrink-0 text-xs text-gray-500">serving backend</span>
          <span className="font-mono text-gray-200">{settings.inference_backend}</span>
          <span className="text-[10px] uppercase tracking-wide text-gray-600">boot fact · read-only</span>
        </div>

        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-3 text-sm">
            <span className="w-36 shrink-0 text-xs text-gray-500">Run:ai</span>
            <Toggle on={runaiLicensed} onToggle={() => setRunaiLicensed((v) => !v)} label="Run:ai licensed (declared)" />
            {/* The honesty label (spec §8): a DECLARED fact, never presented as
                verified. No bare "licensed" badge — that would claim routeForge
                confirmed something it never checks. */}
            {runaiLicensed ? (
              <span className="font-mono text-xs text-privacy">Run:ai — declared licensed</span>
            ) : (
              <span className="text-xs text-gray-500">not declared licensed</span>
            )}
          </div>
          <p className="ml-36 pl-3 text-[10px] text-gray-600">
            Declared by an admin, not verified — routeForge never probes the cluster or calls Run:ai.
          </p>
        </div>

        <label className="flex items-center gap-3 text-sm">
          <span className="w-36 shrink-0 text-xs text-gray-500">Run:ai project</span>
          <input
            value={runaiProject}
            onChange={(e) => setRunaiProject(e.target.value)}
            placeholder="(none)"
            className="w-56 rounded bg-ink border border-edge px-2 py-1 text-sm font-mono text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          />
        </label>

        <div className="flex flex-col gap-1">
          <label className="flex items-center gap-3 text-sm">
            <span className="w-36 shrink-0 text-xs text-gray-500">default engine</span>
            <EngineSelect value={defaultEngine} onChange={setDefaultEngine} />
          </label>
          {/* Engine choice only has effect under Dynamo (the one backend that can
              front a mixed vLLM + TensorRT-LLM fleet). Under vllm/none the engine
              is a cluster fact, so say so rather than imply this select does work. */}
          <p className="ml-36 pl-3 text-[10px] text-gray-600">
            {settings.inference_backend === "dynamo"
              ? "Fallback engine for fleet models with no per-model engine set."
              : `Applies only under Dynamo — ignored while the serving backend is ${settings.inference_backend}.`}
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={save}
            disabled={busy}
            className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1.5 text-sm disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {busy ? "saving…" : "Save"}
          </button>
          {saved && !busy && <span className="text-xs text-cheap">saved</span>}
          {saveErr && <span className="text-xs text-frontier">save failed — default engine must be vllm or trtllm</span>}
        </div>
      </div>
    </Panel>
  );
}

// --- On-prem fleet table -----------------------------------------------------

const CROSS_CHECK: Record<InfraModel["cross_check"], { cls: string; mark: string; label: string }> = {
  serving: { cls: "text-cheap", mark: "●", label: "match" },
  absent: { cls: "text-frontier", mark: "▲", label: "not serving" },
  // alert red reserved for this ONE genuine health-fail state in this panel.
  unreachable: { cls: "text-alert", mark: "●", label: "unreachable" },
};

function CrossCheckBadge({ status }: { status: InfraModel["cross_check"] }) {
  const c = CROSS_CHECK[status];
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap font-mono text-xs ${c.cls}`}>
      <span aria-hidden="true">{c.mark}</span>
      {c.label}
    </span>
  );
}

function ModelRow({
  model,
  backend,
  onSaved,
}: {
  model: InfraModel;
  backend: string | null;
  onSaved: () => void;
}) {
  const [engine, setEngine] = useState(model.engine ?? "vllm");
  const [enabled, setEnabled] = useState(model.enabled);
  const [replicas, setReplicas] = useState<number | null>(model.replicas);
  const [tensorParallel, setTensorParallel] = useState<number | null>(model.tensor_parallel);
  const [gpuType, setGpuType] = useState(model.gpu_type ?? "");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState(false);

  const dirty =
    engine !== (model.engine ?? "vllm") ||
    enabled !== model.enabled ||
    replicas !== model.replicas ||
    tensorParallel !== model.tensor_parallel ||
    gpuType !== (model.gpu_type ?? "");

  async function save() {
    setBusy(true);
    setErr(false);
    setSaved(false);
    try {
      await infraApi.putModel(model.model_name, {
        engine,
        enabled,
        replicas,
        tensor_parallel: tensorParallel,
        gpu_type: gpuType.trim() ? gpuType.trim() : null,
      });
      setSaved(true);
      onSaved();
    } catch {
      setErr(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    // The dim-node metaphor (spec §12): a disabled row dims exactly like a
    // rejected/excluded candidate in the junction inspector — same idiom as
    // RequestDrilldown's rejected spurs (`bg-ink/40 ... opacity-60`), not just
    // faded text.
    <tr
      className={`border-b border-edge/60 last:border-0 transition-colors ${
        enabled ? "" : "bg-ink/40 opacity-60"
      }`}
    >
      <td className="py-2 pr-3 align-top">
        <div className="text-sm text-gray-100">{model.display_name}</div>
        <div className="font-mono text-xs text-gray-500">{model.model_name}</div>
        {!model.managed && (
          <span className="mt-1 inline-block rounded border border-edge px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-gray-500">
            unmanaged / defaulted
          </span>
        )}
      </td>
      <td className="py-2 pr-3 align-top">
        <span className="flex items-center gap-1.5">
          <TierDot tier={model.tier} />
          <span className="text-xs text-gray-400">{model.tier}</span>
        </span>
      </td>
      <td className="py-2 pr-3 align-top">
        {/* Engine is only a per-model choice under Dynamo — the one backend that
            can front a mixed vLLM + TensorRT-LLM fleet. Under vllm every model is
            served by vLLM (engine_for returns "vllm" by construction); under none
            on-prem serving is off. In those cases the select would be inert, so we
            render the effective engine read-only instead of a control that lies. */}
        {backend === "dynamo" ? (
          <EngineSelect value={engine} onChange={setEngine} ariaLabel={`${model.display_name} engine`} />
        ) : (
          <span
            className="inline-flex items-center font-mono text-xs text-gray-400"
            title={
              backend === "none"
                ? "On-prem serving is disabled (backend: none)"
                : "Engine is cluster-level under vLLM — per-model engine applies only under Dynamo"
            }
          >
            {backend === "none" ? "—" : "vLLM"}
          </span>
        )}
      </td>
      <td className="py-2 pr-3 align-top">
        <Toggle on={enabled} onToggle={() => setEnabled((v) => !v)} label={`${model.display_name} enabled`} />
      </td>
      <td className="py-2 pr-3 align-top">
        <input
          type="number"
          min={0}
          value={replicas ?? ""}
          onChange={(e) => setReplicas(e.target.value ? Number(e.target.value) : null)}
          aria-label={`${model.display_name} replicas`}
          className="w-14 rounded bg-ink border border-edge px-1.5 py-1 font-mono text-xs text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </td>
      <td className="py-2 pr-3 align-top">
        <input
          type="number"
          min={1}
          value={tensorParallel ?? ""}
          onChange={(e) => setTensorParallel(e.target.value ? Number(e.target.value) : null)}
          aria-label={`${model.display_name} tensor parallel`}
          className="w-12 rounded bg-ink border border-edge px-1.5 py-1 font-mono text-xs text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </td>
      <td className="py-2 pr-3 align-top">
        <input
          value={gpuType}
          onChange={(e) => setGpuType(e.target.value)}
          placeholder="(none)"
          aria-label={`${model.display_name} GPU type`}
          className="w-24 rounded bg-ink border border-edge px-1.5 py-1 font-mono text-xs text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </td>
      <td className="py-2 pr-3 align-top">
        <CrossCheckBadge status={model.cross_check} />
      </td>
      <td className="py-2 align-top">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={save}
            disabled={busy || !dirty}
            className="rounded border border-accent bg-accent/20 text-accent px-2 py-1 text-xs disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {busy ? "saving…" : "Save"}
          </button>
          {saved && !dirty && !busy && <span className="text-xs text-cheap">saved</span>}
          {err && <span className="text-xs text-frontier">failed</span>}
        </div>
      </td>
    </tr>
  );
}

function FleetTable() {
  const [models, setModels] = useState<InfraModel[] | null>(null);
  // The cluster serving backend gates whether per-model engine is editable — it
  // lives in the same /admin/infra settings the Cluster card reads, so fetch it
  // alongside the roster rather than threading it through a shared parent.
  const [backend, setBackend] = useState<string | null>(null);
  const [err, setErr] = useState(false);

  async function load() {
    try {
      const [rows, settings] = await Promise.all([infraApi.models(), infraApi.get()]);
      setModels(rows);
      setBackend(settings.inference_backend);
      setErr(false);
    } catch {
      setErr(true);
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <Panel title="On-prem fleet">
      {err && <p className="text-sm text-frontier">Could not load the on-prem fleet.</p>}
      {!err && models === null && <p className="text-sm text-gray-500">Loading…</p>}
      {!err && models !== null && models.length === 0 && (
        <p className="text-sm text-gray-500">No on-prem models in the pool.</p>
      )}
      {!err && models !== null && models.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] border-collapse text-left">
            <thead>
              <tr className="border-b border-edge text-[10px] uppercase tracking-wide text-gray-500">
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Model</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Tier</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Engine</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Enabled</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Replicas</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">TP</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">GPU</th>
                <th scope="col" className="py-2 pr-3 font-display font-semibold">Cross-check</th>
                <th scope="col" className="py-2 font-display font-semibold">&nbsp;</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <ModelRow key={m.model_name} model={m} backend={backend} onSaved={load} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

export function InfrastructurePanel() {
  return (
    <div className="flex flex-col gap-4">
      <ClusterCard />
      <FleetTable />
    </div>
  );
}
