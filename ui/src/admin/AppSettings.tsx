// The per-application Settings tab, as six numbered collapsible steps.
//
// It used to be eight flat regions in one ~1800px scroll, every field expanded
// at the same visual weight with permanently-visible explanatory prose (see
// docs/superpowers/plans/2026-09-05-settings-ux-plan.md). Same controls, same
// endpoints, same server clamps — reorganised so an operator can answer "what
// is this app set to?" without reading the page, and configure one concern at
// a time:
//
//   1 Access            tokens                       POST/DELETE .../tokens
//   2 Placement         residency, providers, …  ┐
//   3 Protection        policy_mode + detection  ├─  PUT .../policy (one call)
//   4 Telemetry         capture_io + Langfuse    ┤   (+ PUT .../langfuse)
//   5 Delivery & limits stream, Headroom, quotas ┘
//   6 Privacy rules     recognizers + rules          PUT .../privacy
//
// Two rules this file must keep:
//
//   * THE SERVER CLAMPS ARE THE ONLY AUTHORITY. Everything here mirrors
//     `set_policy` (app/api/admin_routes.py) or explains it; nothing re-encodes
//     a rule locally (bug B22). Where an option is refused, the reason is shown
//     and the 422 detail from a real save is still surfaced verbatim (B23).
//   * `passthrough` must never appear to touch placement. Protection and
//     placement are separate axes (CLAUDE.md rule 2a) and are separate steps.
import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { adminApi, getPrivacy } from "../api";
import type { AdminApp, AdminPolicy, DeploymentEnvelope } from "../api";
import { BUILTIN_ENTITIES, PrivacyRulesEditor } from "./PrivacyRulesEditor";
import { TokenReveal } from "./clipboard";

const TIERS = ["cheap", "mid", "frontier"];
const PROVIDERS = ["onprem", "azure", "bedrock"];
export const RESIDENCIES = ["any", "private_cloud_ok", "onprem_only"];
// Per-app override vocabularies — mirror app/policy/enums.py exactly
// (PII_MODES / STREAM_MODES / POLICY_MODES). Exported so the protection and
// streaming controls can reuse them without respelling the values.
export const PII_MODES = ["off", "regex", "presidio"];
export const STREAM_MODES = ["off", "verified_then_stream", "stream_unverified", "incremental_verify"];
export const POLICY_MODES = ["enforced", "passthrough"];

// The one entity type that can never be suppressed by any per-app tuning:
// PROTECTED_ENTITIES in app/policy/effective.py. Naming it in
// pii_entity_denylist is a 422, and no threshold or exempt pattern can drop it
// (CLAUDE.md rules 3/8). Shown in the chip row, never settable.
const PROTECTED_ENTITIES = ["US_SSN"];
// Mirrors MAX_EXEMPT_PATTERNS / MAX_EXEMPT_PATTERN_LEN in
// app/api/admin_routes.py. Surfaced here so the operator sees the limit while
// typing instead of discovering it as a 422.
const MAX_EXEMPT_PATTERNS = 25;
const MAX_EXEMPT_PATTERN_LEN = 200;

// --- shared class vocabulary (the console's existing dark palette) ----------
const labelCls = "text-xs text-gray-500 w-28 shrink-0";
const hintCls = "text-[10px] text-gray-600";
const selectCls =
  "cursor-pointer rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100 " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";
const inputCls =
  "rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";
const btnCls =
  "cursor-pointer rounded-md border border-accent bg-accent/20 text-accent px-3 py-1.5 text-xs " +
  "transition-colors duration-200 disabled:opacity-50 disabled:cursor-not-allowed " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";
const linkBtnCls =
  "cursor-pointer text-xs text-accent hover:underline disabled:opacity-40 disabled:no-underline " +
  "disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded";
const chipCls = (on: boolean) =>
  `cursor-pointer px-2 py-0.5 rounded text-xs border transition-colors duration-200 ` +
  `focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
    on ? "border-accent text-accent bg-accent/10" : "border-edge text-gray-500"
  }`;

function multiToggle(list: string[] | null, value: string): string[] | null {
  const set = new Set(list ?? []);
  set.has(value) ? set.delete(value) : set.add(value);
  const arr = [...set];
  return arr.length ? arr : null; // empty => no restriction
}

// Same toggle, but it never collapses to null: an emptied list is sent as `[]`,
// which stores "nothing exempt" explicitly. (Since `reset`, null would also
// clear the stored list rather than silently re-applying it — but `[]` says
// what it means for a list whose empty state is a real, chosen value.)
function clearableToggle(list: string[] | null, value: string): string[] {
  const set = new Set(list ?? []);
  set.has(value) ? set.delete(value) : set.add(value);
  return [...set];
}

// Client-side echo of the server's pii_exempt_patterns checks (length, then
// compile). JS and Python regex dialects are not identical, so this catches the
// common mistakes early; the server remains the authority and its 422 still
// shows under Save.
function patternError(p: string): string | null {
  if (!p) return null;
  if (p.length > MAX_EXEMPT_PATTERN_LEN) return `too long (>${MAX_EXEMPT_PATTERN_LEN} chars)`;
  try {
    new RegExp(p);
  } catch (e) {
    return e instanceof Error ? e.message : "does not compile";
  }
  return null;
}

// Which fields this save should send in `reset` — the explicit "put it back to
// NULL" list the PUT needs, because the PUT itself is a merge and a null in the
// body means "leave the stored value alone".
//
// Derived, never hand-maintained: a field belongs in `reset` exactly when the
// app has a value STORED for it and the operator has moved the control back to
// its "(global default)" / empty state. Because every field in the list is null
// in the body, the two can never contradict each other (the server 422s if they
// do). Keyed off the stored policy object, so a new column added to AdminPolicy
// becomes resettable with no change here.
function resetList(stored: AdminPolicy | null, next: AdminPolicy): string[] {
  if (!stored) return [];
  return (Object.keys(stored) as (keyof AdminPolicy)[])
    .filter((k) => stored[k] !== null && stored[k] !== undefined && next[k] === null);
}

// Which numbered step owns which policy field. Used for the per-step dirty
// dots — the PUT is one call for all four steps, so the dot is what tells the
// operator where their unsaved edits actually are. Every AdminPolicy field
// appears exactly once; a new column with no entry simply shows no dot, never a
// wrong one.
const STEP_FIELDS: Record<number, (keyof AdminPolicy)[]> = {
  2: ["residency", "allowed_providers", "allowed_tiers", "allowed_models"],
  3: ["policy_mode", "pii_detection", "pii_score_threshold", "pii_entity_denylist", "pii_exempt_patterns"],
  4: ["capture_io"],
  5: ["stream_mode", "compression_enabled", "max_rpm", "max_spend_usd_per_day", "max_cost_usd_per_request"],
};

// Order-insensitive comparison, so re-toggling a chip back to where it started
// doesn't leave a phantom dirty dot.
function sameValue(a: unknown, b: unknown): boolean {
  const norm = (v: unknown) =>
    JSON.stringify(Array.isArray(v) ? [...(v as string[])].sort() : (v ?? null));
  return norm(a) === norm(b);
}

function dirtyFields(stored: AdminPolicy | null, next: AdminPolicy, fields: (keyof AdminPolicy)[]): boolean {
  return fields.some((k) => !sameValue(stored ? stored[k] ?? null : null, next[k] ?? null));
}

// --- disclosure primitives -------------------------------------------------

// Per-field help. ONE short line stays inline next to every control; the longer
// explanation — the hard-won product guidance about what the server clamps do
// and why — moves in here. Nothing was deleted, only relocated.
function Help({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        aria-label={`${open ? "Hide" : "Show"} help for ${label}`}
        onClick={() => setOpen(!open)}
        className="inline-flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded-full border border-edge text-[9px] text-gray-500 transition-colors duration-200 hover:border-accent hover:text-gray-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
        ?
      </button>
      <div
        id={id}
        className={
          open
            ? "basis-full rounded border border-edge/60 bg-ink/40 px-2 py-1.5 text-[10px] leading-relaxed text-gray-500"
            : "hidden"
        }>
        {children}
      </div>
    </>
  );
}

// One labelled control row: label, control, one short inline hint, `?` help.
function Row({ label, htmlFor, hint, help, children, align = "center" }: {
  label: string;
  htmlFor?: string;
  hint?: ReactNode;
  help?: ReactNode;
  children: ReactNode;
  align?: "center" | "start";
}) {
  return (
    <div className={`flex flex-wrap gap-2 ${align === "start" ? "items-start" : "items-center"}`}>
      {htmlFor ? (
        <label htmlFor={htmlFor} className={`${labelCls} ${align === "start" ? "pt-1" : ""}`}>{label}</label>
      ) : (
        // Chip groups have no single focusable control to point a <label> at —
        // the group carries its own accessible name via role="group".
        <span aria-hidden className={`${labelCls} ${align === "start" ? "pt-1" : ""}`}>{label}</span>
      )}
      {children}
      {hint && <span className={hintCls}>{hint}</span>}
      {help && <Help label={label}>{help}</Help>}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 12 12"
      className={`h-3 w-3 shrink-0 text-gray-500 transition-transform duration-200 ${open ? "rotate-180" : ""}`}>
      <path d="M2 4.5 L6 8.5 L10 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

// A numbered step: collapsed by default, showing a one-line summary of its
// current values so the whole page is scannable without opening anything.
function Step({ n, title, summary, open, onToggle, dirty, advanced, children }: {
  n: number;
  title: string;
  summary: string;
  open: boolean;
  onToggle: () => void;
  dirty?: boolean;
  advanced?: boolean;
  children: ReactNode;
}) {
  const uid = useId();
  const headId = `${uid}-head`;
  const bodyId = `${uid}-body`;
  return (
    <section className="rounded-md border border-edge bg-panel/40">
      <button
        type="button"
        id={headId}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
        className="flex w-full cursor-pointer items-center gap-3 rounded-md px-3 py-2.5 text-left transition-colors duration-200 hover:bg-edge/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset">
        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded border border-edge font-mono text-[11px] text-gray-400">
          {n}
        </span>
        <span className="shrink-0 font-display text-sm text-gray-100">{title}</span>
        {dirty && (
          <span className="flex shrink-0 items-center gap-1">
            <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-accent" />
            <span className="sr-only">unsaved changes</span>
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-xs text-gray-400">{summary}</span>
        {advanced && (
          <span className="shrink-0 rounded border border-edge px-1 py-0.5 text-[9px] uppercase tracking-wide text-gray-500">
            advanced
          </span>
        )}
        <span className="shrink-0 text-xs text-accent">{open ? "close" : "edit"}</span>
        <Chevron open={open} />
      </button>
      <div
        id={bodyId}
        role="region"
        aria-labelledby={headId}
        className={open ? "flex flex-col gap-3 border-t border-edge/60 px-3 py-3 text-sm" : "hidden"}>
        {children}
      </div>
    </section>
  );
}

// A collapsible sub-group inside a step. Its one job today: the Detection &
// narrowing block, which is INERT under policy_mode = passthrough and used to
// render a full screen of dimmed controls there.
function SubGroup({ title, summary, open, onToggle, tone = "normal", children }: {
  title: string;
  summary: string;
  open: boolean;
  onToggle: () => void;
  tone?: "normal" | "inert";
  children: ReactNode;
}) {
  const uid = useId();
  const bodyId = `${uid}-sub`;
  return (
    <div className="rounded border border-edge/50">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
        className="flex w-full cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-left transition-colors duration-200 hover:bg-edge/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset">
        <span className="shrink-0 text-[10px] uppercase tracking-wide text-gray-500">{title}</span>
        <span className={`min-w-0 flex-1 truncate text-[10px] ${tone === "inert" ? "text-frontier" : "text-gray-600"}`}>
          {summary}
        </span>
        <Chevron open={open} />
      </button>
      <div id={bodyId} className={open ? "flex flex-col gap-2 border-t border-edge/40 p-2" : "hidden"}>
        {children}
      </div>
    </div>
  );
}

// --- summary lines ---------------------------------------------------------
// Every one reads the EFFECTIVE value, naming what an unset field inherits from
// the deployment envelope (GET /admin/me) rather than saying "(global default)"
// and leaving the operator to guess. They track the form live, and the strip
// says plainly when what is shown is not yet saved.

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function accessSummary(app: AdminApp): string {
  const active = app.tokens.filter((t) => t.status === "active").length;
  const revoked = app.tokens.length - active;
  const head = active ? plural(active, "active token") : "no active tokens";
  return revoked ? `${head} · ${revoked} revoked` : head;
}

function placementSummary(p: AdminPolicy, app: AdminApp, models: string[]): string {
  const parts = [`${p.residency ?? app.default_residency}${p.residency ? "" : " (app default)"}`];
  parts.push(p.allowed_providers?.length ? `providers: ${p.allowed_providers.join(", ")}` : "all providers");
  parts.push(p.allowed_tiers?.length ? `tiers: ${p.allowed_tiers.join(", ")}` : "all tiers");
  parts.push(models.length ? `${plural(models.length, "model")} pinned` : "all models");
  return parts.join(" · ");
}

function protectionSummary(p: AdminPolicy, effPolicyMode: string, effPii: string | null): string {
  const denylist = p.pii_entity_denylist ?? [];
  const patterns = (p.pii_exempt_patterns ?? []).filter(Boolean);
  const narrowing: string[] = [];
  if (p.pii_score_threshold !== null && p.pii_score_threshold !== undefined) {
    narrowing.push(`min conf ${p.pii_score_threshold}`);
  }
  if (denylist.length) narrowing.push(plural(denylist.length, "exempt type"));
  if (patterns.length) narrowing.push(plural(patterns.length, "exempt value"));

  if (effPolicyMode === "passthrough") {
    const kept = narrowing.length ? ` · ${narrowing.join(" · ")} kept` : "";
    return `passthrough — protection off${p.policy_mode ? "" : " (deployment default)"}${kept}`;
  }
  const bits = [`enforced${p.policy_mode ? "" : " (deployment default)"}`];
  bits.push(`detection ${effPii ?? "deployment default"}${p.pii_detection ? "" : " (inherited)"}`);
  return [...bits, ...narrowing].join(" · ");
}

function telemetrySummary(p: AdminPolicy, app: AdminApp, effCapture: string | null): string {
  const inherited = p.capture_io ? "" : " (deployment default)";
  const dest = !app.langfuse_project_id
    ? "shared project"
    : app.has_langfuse_keys === false
      ? "own project — credentials missing"
      : "own Langfuse project ✓";
  if (effCapture === "off") return `capture off${inherited} — no request I/O on traces`;
  if (!effCapture) return `capture unknown → ${dest}`;
  return `${effCapture}${inherited} → ${dest}`;
}

function deliverySummary(p: AdminPolicy, dep: DeploymentEnvelope | null): string {
  const noOptIn = !p.stream_mode || p.stream_mode === "off";
  const stream = noOptIn
    ? dep && !dep.reject_stream_requests
      ? "stream: true served unstreamed"
      : "no streaming"
    : p.stream_mode;
  const headroom =
    p.compression_enabled === null || p.compression_enabled === undefined
      ? "Headroom inherit"
      : p.compression_enabled
        ? "Headroom on"
        : "Headroom off";
  const limits: string[] = [];
  if (p.max_rpm) limits.push(`${p.max_rpm} rpm`);
  if (p.max_spend_usd_per_day) limits.push(`$${p.max_spend_usd_per_day}/day`);
  if (p.max_cost_usd_per_request) limits.push(`$${p.max_cost_usd_per_request}/request`);
  return [stream, headroom, limits.length ? limits.join(" · ") : "no limits"].join(" · ");
}

// --- sample-value tester (unchanged behaviour) ------------------------------
// Sample-value preview for pii_exempt_patterns, in the idiom of the Privacy
// tab's "Test against sample text" box — but a different test, because the
// server applies a different operation: a pattern is `re.fullmatch`ed against
// the *detected entity's value*, not searched over the prompt. So the input
// here is one candidate value per line, and the verdict per line is binary.
// `^(?:…)$` is the browser's closest approximation of Python's fullmatch.
function ExemptPatternTester({ patterns }: { patterns: string[] }) {
  const [text, setText] = useState("");
  const uid = useId();
  const compiled: RegExp[] = [];
  for (const p of patterns) {
    if (!p || patternError(p)) continue;
    try {
      compiled.push(new RegExp(`^(?:${p})$`));
    } catch {
      // already reported inline by patternError
    }
  }
  const lines = text.split("\n").filter((l) => l.trim().length > 0);

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={uid} className="text-[10px] uppercase tracking-wide text-gray-500">
        Test candidate values
      </label>
      <textarea
        id={uid}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="One candidate value per line, e.g. 12 3456 789A — checked in your browser only, nothing is sent anywhere."
        rows={2}
        className={`${inputCls} w-full`}
      />
      {lines.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {lines.map((l, i) => {
            const exempt = compiled.some((re) => re.test(l));
            return (
              <li key={i} className="flex items-center gap-2 text-xs">
                <code className="font-mono text-gray-300 truncate">{l}</code>
                <span className={exempt ? "text-cheap" : "text-gray-500"}>
                  {exempt ? "exempt — reaches the model verbatim" : "not exempt — redacted if detected"}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      <p className={hintCls}>
        Approximates the server's <code className="font-mono">re.fullmatch</code> against the detected
        entity's value. A US_SSN match is never exempted, however this reads.
      </p>
    </div>
  );
}

// --- Langfuse credentials (step 4) -----------------------------------------
// Manual per-app Langfuse creds (B15) — attach an existing Langfuse project by
// hand. This is the primary path on OSS Langfuse (org-scoped auto-provisioning
// needs an Enterprise license); it is also how air-gapped operators wire a
// project provisioned out of band. Once saved, `app.langfuse_project_id` is set,
// which is what the `full`-capture gate checks — so `onSaved` must refetch the
// app for that gate to unlock. It now sits directly above `capture_io` for that
// reason: the two were ~1000px apart while one gates the other. Fields reset
// after a successful save; the secret is write-only and is never rendered back,
// before or after saving.
function LangfuseCredsForm({ app, onSaved }: { app: AdminApp; onSaved: () => void }) {
  const [host, setHost] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [projectId, setProjectId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const uid = useId();

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      await adminApi.setLangfuseCreds(app.id, {
        host, public_key: publicKey, secret_key: secretKey, project_id: projectId,
      });
      setHost(""); setPublicKey(""); setSecretKey(""); setProjectId("");
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not save credentials.");
    } finally {
      setBusy(false);
    }
  }

  const canSave = host && publicKey && secretKey && projectId && !busy;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
        <span>
          Send this app's traces to its own Langfuse project.{" "}
          {app.has_langfuse_keys
            ? <span className="text-cheap">credentials set ✓</span>
            : <span className="text-gray-600">not set — this app shares the default project</span>}
        </span>
        <Help label="Langfuse credentials">
          Create the project in Langfuse first, then paste its <strong>project</strong>-scoped keys
          below. Use the keys from <em>Project → Settings → API Keys</em> (not organization keys).
          Leave blank to keep sharing the default project. Raw (<code className="font-mono">full</code>)
          capture is only ever permitted once this app has its own project — the shared project never
          receives raw I/O.
        </Help>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs text-gray-400">
          host
          <input id={`${uid}-host`} value={host} onChange={(e) => setHost(e.target.value)}
            placeholder="http://langfuse-web:3000"
            title="Server-reachable Langfuse (in-cluster, e.g. http://langfuse-web:3000) — not the browser URL"
            className={`${inputCls} w-48`} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-400">
          public key
          <input id={`${uid}-pk`} value={publicKey} onChange={(e) => setPublicKey(e.target.value)}
            placeholder="pk-lf-…" className={`${inputCls} w-36`} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-400">
          secret key
          <input id={`${uid}-sk`} type="password" value={secretKey} onChange={(e) => setSecretKey(e.target.value)}
            autoComplete="off" placeholder={app.has_langfuse_keys ? "(unchanged — enter to replace)" : "sk-lf-…"}
            className={`${inputCls} w-36`} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-400">
          project id
          <input id={`${uid}-pid`} value={projectId} onChange={(e) => setProjectId(e.target.value)}
            placeholder="cmt5…"
            title="The project's id (e.g. cmt5…) — the project settings URL, or GET {host}/api/public/projects"
            className={`${inputCls} w-32`} />
        </label>
        <button type="button" onClick={save} disabled={!canSave} className={`${btnCls} py-1`}>
          {busy ? "saving…" : "Save credentials"}
        </button>
        {err && <span className="basis-full text-xs text-frontier">{err}</span>}
      </div>
    </div>
  );
}

// --- the policy save, shown in each of steps 2–5 ---------------------------
// The PUT is one call covering all four steps, so the button says so and names
// which other steps it will carry along. The server's 422 detail is rendered
// verbatim: it names the clamp that fired and the way out, and is the
// operator's only guidance here (bug B23 — do not swallow it).
function PolicySave({ busy, err, onSave, pendingReset, alsoSaving }: {
  busy: boolean;
  err: string | null;
  onSave: () => void;
  pendingReset: string[];
  alsoSaving: number[];
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-t border-edge/60 pt-3">
      <button type="button" onClick={onSave} disabled={busy} className={btnCls}>
        {busy ? "saving…" : "Save policy"}
      </button>
      <span className={hintCls}>
        One call covers steps 2–5 (placement, protection, telemetry, delivery).
        {alsoSaving.length > 0 && (
          <span className="text-gray-500"> Also saves unsaved edits in step{alsoSaving.length > 1 ? "s" : ""} {alsoSaving.join(", ")}.</span>
        )}
      </span>
      <Help label="saving">
        Saving merges: a control you don't touch keeps whatever this app has stored. Moving a
        control back to its “(global default)” option is the exception — that field is cleared,
        and the app inherits the deployment default again. Placement, protection, telemetry and
        delivery are one row server-side and one PUT, so a save from any of steps 2–5 writes all
        four together.
      </Help>
      {/* Clearing a field is a real change with real consequences (dropping
          policy_mode hands the app back to the deployment's posture), so it
          is named before the save, not discovered afterwards. */}
      {pendingReset.length > 0 && (
        <span className="basis-full text-[10px] text-gray-500">
          Saving clears{" "}
          {pendingReset.map((f, i) => (
            <span key={f}>
              {i > 0 && ", "}
              <code className="font-mono text-gray-400">{f}</code>
            </span>
          ))}{" "}
          back to the deployment default. Refused with a reason if that would leave the app in a
          posture the clamps forbid — e.g. clearing <code className="font-mono">policy_mode</code>{" "}
          on an onprem_only app that is storing raw I/O.
        </span>
      )}
      {err && <span className="basis-full text-xs text-frontier">{err}</span>}
    </div>
  );
}

// --- the recipes, made executable ------------------------------------------
// docs/guides/choosing-app-settings.md §2 is the contract; this is that section
// with a button on it. The names and the two values each recipe sets are that
// page's, verbatim — if they ever disagree, the doc is right and this is a bug.
//
// A recipe writes exactly TWO fields: `policy_mode` (what the gate does to the
// data) and `capture_io` (what the trace carries). It never touches placement —
// residency, providers, tiers and pinned models are the doc's Q2, answered on
// the customer's contract and never inferred from a protection posture
// (CLAUDE.md rule 2a). It never clears the narrow levers either
// (pii_entity_denylist / pii_score_threshold / pii_exempt_patterns, and step 6's
// recognizers): those survive a posture change today, and a picker that threw
// away an afternoon of tuning would be the worst thing on this page.
//
// And it does not save. It fills the form, the operator reviews the opened
// steps, then presses one of the Save policy buttons — so every clamp is still
// judged by the server, on the whole post-write state, exactly as before.
type Recipe = {
  id: "A" | "B" | "C";
  label: string;
  sets: { policy_mode: string; capture_io: string };
  blurb: string;
};

const RECIPES: Recipe[] = [
  {
    id: "A",
    label: "A — Protected",
    sets: { policy_mode: "enforced", capture_io: "redacted" },
    blurb: "Any app whose input may hold real personal data. The default — start here unless someone gives you a reason otherwise.",
  },
  {
    id: "B",
    label: "B — Router only — no PII checks",
    sets: { policy_mode: "passthrough", capture_io: "full" },
    blurb: "The customer's own data, every field load-bearing output. Nothing is detected or redacted in either direction, and this app's own Langfuse project gets the real text.",
  },
  {
    id: "C",
    label: "C — Protected, no telemetry I/O",
    sets: { policy_mode: "enforced", capture_io: "off" },
    blurb: "Air-gapped, or “don't log our text anywhere”. Traces keep the decision record, cost, latency and candidates — and carry no prompt or completion.",
  },
];

// One recipe option. Same palette and focus ring as the chips and steps above;
// a row rather than a chip because each option carries its values, a blurb and
// any prerequisite it has.
const recipeOptCls = (on: boolean) =>
  "flex w-full cursor-pointer flex-col gap-1 rounded border px-2.5 py-2 text-left " +
  "transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-accent " +
  (on ? "border-accent bg-accent/10" : "border-edge/60 hover:bg-edge/20");

// How a form value reads in the preview: an unset field is not "null", it is
// whatever the deployment envelope says it inherits.
const shownValue = (v: string | null, def: string | undefined) =>
  v ?? `(global default${def ? ` — ${def}` : ""})`;

// What applying this recipe would change, computed against the CURRENT FORM
// state — not the saved policy. The operator may already have edits in flight,
// and a preview measured against the stored row would lie about them. A field
// whose form value already equals the recipe's is omitted; if both are, the
// recipe is a no-op and Apply is disabled.
function recipeChanges(r: Recipe, p: AdminPolicy, dep: DeploymentEnvelope | null) {
  const rows: { field: string; from: string; to: string }[] = [];
  if (p.policy_mode !== r.sets.policy_mode) {
    rows.push({
      field: "policy_mode",
      from: shownValue(p.policy_mode, dep?.default_policy_mode),
      to: r.sets.policy_mode,
    });
  }
  if (p.capture_io !== r.sets.capture_io) {
    rows.push({
      field: "capture_io",
      from: shownValue(p.capture_io, dep?.default_capture_io),
      to: r.sets.capture_io,
    });
  }
  return rows;
}

function RecipePicker({ p, deployment, effPolicyMode, effCapture, warn, onApply }: {
  p: AdminPolicy;
  deployment: DeploymentEnvelope | null;
  effPolicyMode: string;
  effCapture: string | null;
  // Prerequisites and ceilings for a recipe, supplied by the caller so all
  // clamp-mirroring stays in one place next to the other clamps.
  warn: (r: Recipe) => ReactNode[];
  onApply: (r: Recipe) => void;
}) {
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState<Recipe["id"] | null>(null);
  const uid = useId();
  const headId = `${uid}-head`;
  const bodyId = `${uid}-body`;
  // Roving tabindex over the radiogroup: one tab stop, arrows move and select.
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  function onKey(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    const step = e.key === "ArrowDown" || e.key === "ArrowRight" ? 1
      : e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1
        : 0;
    if (!step) return;
    e.preventDefault();
    const j = (i + step + RECIPES.length) % RECIPES.length;
    setSel(RECIPES[j].id);
    refs.current[j]?.focus();
  }

  // The app's CURRENT posture, read off the effective values — what it actually
  // resolves to today, inherited defaults included.
  const isCurrent = (r: Recipe) =>
    effPolicyMode === r.sets.policy_mode && effCapture === r.sets.capture_io;
  const current = RECIPES.find(isCurrent) ?? null;

  const selected = RECIPES.find((r) => r.id === sel) ?? null;
  const changes = selected ? recipeChanges(selected, p, deployment) : [];
  const focusable = sel ?? RECIPES[0].id;

  return (
    <section className="rounded-md border border-edge bg-panel/40">
      <button
        type="button"
        id={headId}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
        className="flex w-full cursor-pointer items-center gap-3 rounded-md px-3 py-2.5 text-left transition-colors duration-200 hover:bg-edge/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset">
        <span className="shrink-0 font-display text-sm text-gray-100">Start from a recipe</span>
        <span className="min-w-0 flex-1 truncate text-xs text-gray-400">
          {current
            ? `currently ${current.label}`
            : "the three postures from the settings guide — fills the form, saves nothing"}
        </span>
        <span className="shrink-0 text-xs text-accent">{open ? "close" : "choose"}</span>
        <Chevron open={open} />
      </button>
      <div
        id={bodyId}
        role="region"
        aria-labelledby={headId}
        className={open ? "flex flex-col gap-3 border-t border-edge/60 px-3 py-3" : "hidden"}>
        <p className={hintCls}>
          Recipes set protection and telemetry only. Placement (step 2) is a separate decision and is
          never changed. Choosing one fills the form — nothing is written until you press Save policy.
        </p>

        <div role="radiogroup" aria-label="Start from a recipe" className="flex flex-col gap-1.5">
          {RECIPES.map((r, i) => {
            const warnings = warn(r);
            return (
              <button
                key={r.id}
                type="button"
                role="radio"
                aria-checked={sel === r.id}
                ref={(el) => { refs.current[i] = el; }}
                tabIndex={focusable === r.id ? 0 : -1}
                onKeyDown={(e) => onKey(e, i)}
                onClick={() => setSel(r.id)}
                className={recipeOptCls(sel === r.id)}>
                <span className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-gray-100">{r.label}</span>
                  {isCurrent(r) && (
                    <span className="rounded border border-edge px-1 py-0.5 text-[9px] uppercase tracking-wide text-cheap">
                      this app's current configuration
                    </span>
                  )}
                </span>
                <span className="font-mono text-[10px] text-gray-500">
                  policy_mode: {r.sets.policy_mode} · capture_io: {r.sets.capture_io}
                </span>
                <span className="text-[10px] leading-relaxed text-gray-500">{r.blurb}</span>
                {/* Prerequisites and ceilings, stated up front — never enforced
                    here. The Apply button stays enabled and the server is still
                    the one that refuses the write, with its 422 detail under
                    Save (B22: the console explains a clamp, it never becomes a
                    second authority for it). */}
                {warnings.map((w, k) => (
                  <span key={k} className="text-[10px] leading-relaxed text-frontier">{w}</span>
                ))}
              </button>
            );
          })}
        </div>

        {selected && (
          <div className="rounded border border-edge/60 bg-ink/40 px-2.5 py-2">
            {changes.length === 0 ? (
              <p className="text-xs text-gray-500">
                The form already matches {selected.label} — nothing to change.
              </p>
            ) : (
              <>
                <p className="text-[10px] uppercase tracking-wide text-gray-500">Will change</p>
                <ul className="mt-1 flex flex-col gap-0.5">
                  {changes.map((c) => (
                    <li key={c.field} className="flex flex-wrap items-center gap-1.5 font-mono text-xs">
                      <span className="text-gray-400">{c.field}</span>
                      <span className="text-gray-500">{c.from}</span>
                      <span aria-hidden className="text-gray-600">→</span>
                      <span className="sr-only">becomes</span>
                      <span className="text-accent">{c.to}</span>
                    </li>
                  ))}
                </ul>
                {/* A recipe writes explicit values, so it can still be a real
                    change on an app that already resolves to that posture by
                    inheritance — and that change is visible: it pins the app
                    against a later move in the deployment default, and it moves
                    app_policy_version. Say so rather than letting the preview
                    look like it contradicts the “current configuration” tag. */}
                {isCurrent(selected) && (
                  <p className="mt-1.5 text-[10px] leading-relaxed text-gray-600">
                    This is already the app's effective posture — applying it pins the two values on
                    the app instead of inheriting them, so a later change to the deployment default
                    cannot move this app.
                  </p>
                )}
                <p className="mt-1.5 text-[10px] leading-relaxed text-gray-600">
                  Nothing else moves: residency, providers, tiers and pinned models (step 2), and the
                  detector, exempt types, exempt values and confidence floor (step 3), are left exactly
                  as they are.
                </p>
              </>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => selected && onApply(selected)}
            disabled={!selected || changes.length === 0}
            className={btnCls}>
            Apply to the form
          </button>
          <span className={hintCls}>
            Fills the form only. The steps that changed open below — review them, then Save policy.
          </span>
        </div>
      </div>
    </section>
  );
}

// --- the Settings tab ------------------------------------------------------
// Reusable per-application config UI. Fetches its own copy of the app (there is
// no single-get in adminApi, so it lists and finds by id) and re-fetches after
// every mutation. `onChanged` is an optional passthrough so a parent that keeps
// its own app list (e.g. ApplicationsPanel's left-hand list) can stay in sync.
export function AppConfig({ appId, onChanged }: { appId: string; onChanged?: () => void }) {
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [loading, setLoading] = useState(true);
  const [newToken, setNewToken] = useState<string | null>(null);
  const [deployment, setDeployment] = useState<DeploymentEnvelope | null>(null);
  const [privacyCounts, setPrivacyCounts] = useState<{ recognizers: number; rules: number } | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [rulesMounted, setRulesMounted] = useState(false);
  const uid = useId();

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

  // The deployment envelope names what every unset field inherits. Optional by
  // design: an older server answers /admin/me without it and the summaries fall
  // back to saying nothing rather than guessing.
  useEffect(() => {
    let cancelled = false;
    adminApi.me()
      .then((m) => !cancelled && setDeployment(m.deployment ?? null))
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  // Only for step 6's summary line — the editor itself refetches when opened.
  useEffect(() => {
    let cancelled = false;
    setPrivacyCounts(null);
    getPrivacy(appId)
      .then((pr) => !cancelled && setPrivacyCounts({ recognizers: pr.recognizers.length, rules: pr.rules.length }))
      .catch(() => {});
    return () => { cancelled = true; };
  }, [appId]);

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

  const toggle = (n: number) => {
    // Step 6 mounts its editor on first open and then stays mounted (hidden),
    // so collapsing the step never throws away unsaved rule edits.
    if (n === 6 && !open.has(6)) setRulesMounted(true);
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(n) ? next.delete(n) : next.add(n);
      return next;
    });
  };

  if (loading) return <p className="text-sm text-gray-500">Loading…</p>;
  if (!app) return <p className="text-sm text-frontier">Application not found.</p>;

  return (
    <PolicySteps
      key={app.id}
      app={app}
      deployment={deployment}
      uid={uid}
      open={open}
      toggle={toggle}
      onSaved={refresh}
      tokens={{
        add: addToken,
        revoke,
        newToken,
        summary: accessSummary(app),
      }}
      privacy={{
        mounted: rulesMounted,
        summary: privacyCounts === null
          ? "…"
          : privacyCounts.recognizers === 0 && privacyCounts.rules === 0
            ? "global defaults"
            : [
                privacyCounts.recognizers ? plural(privacyCounts.recognizers, "custom recognizer") : null,
                privacyCounts.rules ? plural(privacyCounts.rules, "sensitivity rule") : null,
              ].filter(Boolean).join(" · "),
      }}
    />
  );
}

// Split out so the policy form state can be seeded once per application: it is
// keyed by app.id above, exactly as PolicyEditor used to be. Without a remount
// per application, selecting a different app would leave the previous app's
// values in the form — and now that a null control means "clear the stored
// value", that stale state would compute the reset list against the wrong app's
// stored policy.
function PolicySteps({ app, deployment, uid, open, toggle, onSaved, tokens, privacy }: {
  app: AdminApp;
  deployment: DeploymentEnvelope | null;
  uid: string;
  open: Set<number>;
  toggle: (n: number) => void;
  onSaved: () => void;
  tokens: { add: () => void; revoke: (id: string) => void; newToken: string | null; summary: string };
  privacy: { mounted: boolean; summary: string };
}) {
  const [p, setP] = useState<AdminPolicy>(
    app.policy ?? {
      allowed_tiers: null, allowed_providers: null, allowed_models: null,
      residency: null, max_spend_usd_per_day: null, max_cost_usd_per_request: null, max_rpm: null,
      compression_enabled: null, capture_io: null,
      pii_detection: null, stream_mode: null, policy_mode: null,
      pii_entity_denylist: null, pii_score_threshold: null, pii_exempt_patterns: null,
    });
  const [modelsText, setModelsText] = useState((app.policy?.allowed_models ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // null = follow the posture: the Detection & narrowing group opens with the
  // step under `enforced` (it is the substance of that posture) and stays
  // collapsed to its one-line summary under `passthrough`, where it is inert.
  // Set once the operator opens or closes it by hand, and then it is theirs.
  const [detectionOverride, setDetectionOverride] = useState<boolean | null>(null);

  // The exact policy body this form would send. Used by `save`, and by the
  // render to tell the operator which stored values a save would clear.
  function payload(): AdminPolicy {
    const models = modelsText.split(",").map((m) => m.trim()).filter(Boolean);
    // Blank rows are editing scaffolding; an explicitly emptied list must
    // still be sent as [] (not null) or the merge keeps the stored patterns.
    const patterns = p.pii_exempt_patterns === null
      ? null
      : p.pii_exempt_patterns.map((s) => s.trim()).filter(Boolean);
    return { ...p, allowed_models: models.length ? models : null, pii_exempt_patterns: patterns };
  }

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const next = payload();
      // `reset` is computed from the payload actually being sent, so the
      // normalisations above are reflected in it.
      const reset = resetList(app.policy, next);
      await adminApi.setPolicy(app.id, reset.length ? { ...next, reset } : next);
      onSaved();
    } catch (e) {
      // Every posture clamp is enforced server-side and answers with a 422
      // whose detail names the rule that fired and the way out. Showing it is
      // the whole point — it's the operator's only guidance here.
      setErr(e instanceof Error ? e.message : "Could not save policy.");
    } finally {
      setBusy(false);
    }
  }

  // --- effective values + server clamps, computed once ---------------------
  // Mirror the clamps in set_policy (app/api/admin_routes.py) — never stricter,
  // never looser. The server stays the authority; a 422 surfaces below the Save
  // button, so an option we leave enabled that the server refuses explains
  // itself rather than failing silently.
  //
  // `full` needs the app's OWN Langfuse project: load_app_langfuse(app) is not
  // None == project id + key pair (the host is server-side only, so the UI
  // can't see that half). `has_langfuse_keys` is optional in the type, so an
  // absent value reads as "unknown" and doesn't block.
  const hasOwnProject = !!app.langfuse_project_id && app.has_langfuse_keys !== false;
  const effResidency = p.residency ?? app.default_residency;
  // Mirrors the server's eff_policy_mode: an unset per-app value inherits
  // `serving.default_policy_mode`, which the deployment envelope now names.
  // (Falls back to the conservative half only when the envelope is missing.)
  const effPolicyMode = p.policy_mode ?? deployment?.default_policy_mode ?? "enforced";
  const passthrough = effPolicyMode === "passthrough";
  // Mirrors eff_capture: unset inherits the deployment's global capture_io.
  const effCapture = p.capture_io ?? deployment?.default_capture_io ?? null;
  // Deliberately asymmetric with the two above, exactly as the server is: an
  // unset pii_detection means "inherit the live global detector", and the
  // server does NOT fold that into its downgrade clamp. Named here only for the
  // summary line; the clamp below still keys on the per-app override alone.
  const effPii = p.pii_detection ?? deployment?.default_pii_detection ?? null;
  // `full` is refused for an onprem_only app only while it is still in the
  // `enforced` posture (PR #34 — residency is placement, policy_mode is
  // protection; onprem_only + passthrough + full is valid).
  //
  // Both capture clamps take the posture as a PARAMETER rather than reading
  // `passthrough` directly. One copy of each rule, still — but a recipe sets
  // policy_mode itself, so it must ask "would this be refused AFTER the recipe
  // lands?" and not be told the answer for the posture the app is leaving.
  const fullBlockedFor = (mode: string) => !hasOwnProject
    ? "needs this app's own Langfuse project"
    : effResidency === "onprem_only" && mode !== "passthrough"
      ? "onprem_only + enforced — set policy_mode = passthrough"
      : null;
  const fullBlocked = fullBlockedFor(effPolicyMode);
  // `redacted` needs a detector to redact WITH. THIS control is now
  // server-driven, not a mirror (Task 9 review round 1 — a Fix-round-1
  // Important finding): it used to re-derive `compile_policy`'s `detection`
  // axis resolution client-side and omitted the `allow_pii_downgrade`
  // ceiling clamp (app/policy/compile.py ~line 309), which reassigns
  // `detection` back to the deployment default whenever the ceiling forbids
  // landing on off/regex — INCLUDING when the passthrough PRESET (not just
  // an explicit per-app override) put it there. Concrete drift that produced:
  // `allow_app_pii_downgrade: false` + `policy_mode: passthrough` compiles
  // server-side to the deployment default detector, making `capture_io:
  // redacted` legal — but the old client mirror computed "off" and blocked
  // Save regardless. `app.effective.detection` is the admin GET/PUT
  // response's compiled value (`_effective_dict`, app/api/admin_routes.py) —
  // exactly what `compile_policy` resolved for THIS app's CURRENTLY SAVED
  // policy, ceilings and all. Reading it directly is the fix: no rung to
  // omit, because nothing here re-implements the precedence any more.
  const redactedBlocked = app.effective?.detection === "off"
    ? "pii_detection resolves to off — nothing to redact telemetry with"
    : null;
  // stream_unverified waives step 9 outright, which onprem_only may never do
  // (set_policy 422s on the pair; app/pipeline.py backstops it at runtime).
  const streamUnverifiedBlocked = effResidency === "onprem_only"
    ? "onprem_only can never waive verification"
    : null;
  // The deployment ceiling, named rather than guessed at. Shown, never
  // enforced here: the write is what the server refuses.
  const passthroughCeiling = deployment && !deployment.allow_app_passthrough;
  const piiDowngradeCeiling = deployment && !deployment.allow_app_pii_downgrade;

  // PREDICTIVE ONLY — used below by `recipeWarning` to preview a recipe's
  // effect BEFORE it is saved. Unlike `redactedBlocked` above (which now
  // reads the server's already-compiled `app.effective`, the CURRENTLY SAVED
  // policy), this has to answer for a hypothetical `policy_mode` the app
  // does not have yet, and there is no endpoint that compiles a draft policy
  // — so it stays a client-side approximation of `compile_policy`'s
  // precedence, folding in the SAME `allow_pii_downgrade` ceiling
  // `redactedBlocked` is now driven by server-side, so the two don't drift
  // apart from EACH OTHER even though this one can still drift from the
  // compiler on a rung neither of us has thought of yet. It is advisory text
  // only (see the "Nothing here disables Apply" note below) — the
  // authoritative answer for any saved state is always `app.effective`, and
  // for an unsaved one, Save's own 422.
  const predictedDetectionFor = (mode: string) => {
    const raw = p.pii_detection ?? (mode === "passthrough" ? "off" : (effPii ?? undefined));
    return (!piiDowngradeCeiling || (raw !== "off" && raw !== "regex"))
      ? raw
      : (deployment?.default_pii_detection ?? undefined);
  };
  const predictedRedactedBlockedFor = (mode: string) =>
    predictedDetectionFor(mode) === "off"
      ? "pii_detection resolves to off — nothing to redact telemetry with"
      : null;

  // --- recipes -------------------------------------------------------------
  // What the operator is told BEFORE applying a recipe: the prerequisite it
  // needs and the deployment ceiling that could forbid it. Every line here is
  // one of the predicates above, evaluated at the recipe's OWN posture — a
  // recipe that sets `passthrough` clears the posture half of `fullBlocked` by
  // itself, and one that sets `enforced` clears the passthrough half of
  // `redactedBlocked`, so showing the pre-recipe verdict would warn about a
  // problem the recipe is about to solve.
  //
  // Nothing here disables Apply. The server refuses the write and its 422
  // detail is rendered verbatim under Save (B22/B23): the console's job is to
  // say what will happen, not to become a second authority for the clamp.
  function recipeWarning(r: Recipe): ReactNode[] {
    const mode = r.sets.policy_mode;
    const out: ReactNode[] = [];
    if (mode === "passthrough" && passthroughCeiling) {
      out.push(
        <>
          This deployment sets <code className="font-mono">policy.allow_app_passthrough: false</code>{" "}
          — passthrough is forbidden install-wide, for every app, so this save is refused whatever
          this app's own policy says.
        </>,
      );
    }
    if (r.sets.capture_io === "full") {
      const blocked = fullBlockedFor(mode);
      if (blocked) {
        out.push(
          <>
            {blocked} — add this app's Langfuse credentials in step 4 first, or the save is refused.
          </>,
        );
      }
    }
    if (r.sets.capture_io === "redacted") {
      const blocked = predictedRedactedBlockedFor(mode);
      if (blocked) {
        out.push(
          <>
            {blocked} — change <code className="font-mono">pii detection</code> in step 3 back to the
            deployment default, or take “C — Protected, no telemetry I/O” instead.
          </>,
        );
      }
    }
    return out;
  }

  // Fill the form; never save. The two recipe fields are spread over the
  // current state, so placement, the narrow levers and everything in step 5 are
  // carried through untouched. Then open the steps whose values actually moved,
  // so the operator lands on the changed controls (and their dirty dots) rather
  // than on a collapsed summary line.
  function applyRecipe(r: Recipe) {
    const movedProtection = p.policy_mode !== r.sets.policy_mode;
    const movedTelemetry = p.capture_io !== r.sets.capture_io;
    setP({ ...p, policy_mode: r.sets.policy_mode, capture_io: r.sets.capture_io });
    if (movedProtection && !open.has(3)) toggle(3);
    if (movedTelemetry && !open.has(4)) toggle(4);
  }

  const denylist = p.pii_entity_denylist ?? [];
  const exemptPatterns = p.pii_exempt_patterns ?? [];
  const patternErrors = exemptPatterns.map(patternError);

  function setPattern(i: number, value: string) {
    setP({ ...p, pii_exempt_patterns: exemptPatterns.map((x, idx) => (idx === i ? value : x)) });
  }

  // Stored values a save would clear (see resetList). Recomputed each render so
  // the notice under Save tracks the form live.
  const next = payload();
  const pendingReset = resetList(app.policy, next);
  const dirty: Record<number, boolean> = {
    2: dirtyFields(app.policy, next, STEP_FIELDS[2]),
    3: dirtyFields(app.policy, next, STEP_FIELDS[3]),
    4: dirtyFields(app.policy, next, STEP_FIELDS[4]),
    5: dirtyFields(app.policy, next, STEP_FIELDS[5]),
  };
  const dirtySteps = [2, 3, 4, 5].filter((n) => dirty[n]);
  const saveFor = (step: number) => (
    <PolicySave
      busy={busy}
      err={err}
      onSave={save}
      pendingReset={pendingReset}
      alsoSaving={dirtySteps.filter((n) => n !== step)}
    />
  );

  const models = modelsText.split(",").map((m) => m.trim()).filter(Boolean);
  const detectionOpen = detectionOverride ?? !passthrough;
  const detectionSummary = passthrough
    ? `inert while passthrough — ${
        [
          denylist.length ? plural(denylist.length, "exempt type") : null,
          exemptPatterns.filter(Boolean).length ? plural(exemptPatterns.filter(Boolean).length, "exempt value") : null,
          p.pii_score_threshold !== null && p.pii_score_threshold !== undefined ? `min conf ${p.pii_score_threshold}` : null,
        ].filter(Boolean).join(" · ") || "nothing set"
      } kept`
    : `${p.pii_detection ?? "inherited detector"}${
        denylist.length ? ` · ${plural(denylist.length, "exempt type")}` : ""
      }${exemptPatterns.filter(Boolean).length ? ` · ${plural(exemptPatterns.filter(Boolean).length, "exempt value")}` : ""}`;

  return (
    <div className="flex flex-col gap-3">
      {/* --- summary strip: what is this app set to? ---------------------- */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-edge bg-panel px-3 py-2">
        <span className="font-display text-sm text-gray-100">{app.name}</span>
        <span className="font-mono text-xs text-gray-500">{app.id}</span>
        <span className="flex flex-wrap items-center gap-x-2 text-xs text-gray-300">
          <span className="text-privacy">{effResidency}</span>
          <span aria-hidden className="text-gray-600">·</span>
          <span className={passthrough ? "text-frontier" : "text-gray-300"}>{effPolicyMode}</span>
          <span aria-hidden className="text-gray-600">·</span>
          <span>capture {effCapture ?? "?"}</span>
          <span aria-hidden className="text-gray-600">·</span>
          <span>detection {passthrough ? "off (passthrough)" : effPii ?? "?"}</span>
        </span>
        {dirtySteps.length > 0 && (
          <span className="ml-auto text-xs text-accent">
            unsaved edits in step{dirtySteps.length > 1 ? "s" : ""} {dirtySteps.join(", ")} — shown values are not saved yet
          </span>
        )}
      </div>

      {/* --- start from a recipe ------------------------------------------ */}
      {/* Between the summary strip and step 1: read what the app is set to,
          then start from one of the three documented postures. It fills this
          form by calling setP(...) — the same state every step below edits —
          and saves nothing, so the operator reviews and then presses one of the
          Save policy buttons. It writes policy_mode and capture_io only, and so
          can never widen placement. */}
      <RecipePicker
        p={p}
        deployment={deployment}
        effPolicyMode={effPolicyMode}
        effCapture={effCapture}
        warn={recipeWarning}
        onApply={applyRecipe}
      />

      <Step n={1} title="Access" summary={tokens.summary} open={open.has(1)} onToggle={() => toggle(1)}>
        <ul className="flex flex-col gap-1">
          {app.tokens.map((t) => (
            <li key={t.id} className="flex items-center gap-2 text-sm">
              <code className="font-mono text-gray-300">{t.prefix}</code>
              <span className="text-gray-600">{t.name}</span>
              <span className={t.status === "active" ? "text-cheap text-xs" : "text-gray-600 text-xs"}>{t.status}</span>
              {t.status === "active" && (
                <button type="button" onClick={() => tokens.revoke(t.id)}
                  aria-label={`Revoke token ${t.prefix}`}
                  className="ml-auto cursor-pointer text-xs text-frontier hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded">
                  revoke
                </button>
              )}
            </li>
          ))}
          {app.tokens.length === 0 && <li className="text-xs text-gray-600">No tokens yet.</li>}
        </ul>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={tokens.add} className={linkBtnCls}>+ issue token</button>
          <span className={hintCls}>Applications authenticate with these bearer tokens.</span>
          <Help label="tokens">
            A token is shown exactly once, when it is minted — copy it then; it is stored hashed and
            cannot be shown again. Revoking is immediate and cannot be undone: issue the replacement
            first, roll the caller over, then revoke the old one.
          </Help>
        </div>
        {tokens.newToken && <TokenReveal token={tokens.newToken} />}
      </Step>

      {/* --- 2 PLACEMENT -------------------------------------------------- */}
      <Step n={2} title="Placement" summary={placementSummary(p, app, models)} dirty={dirty[2]}
        open={open.has(2)} onToggle={() => toggle(2)}>
        <Row label="residency" htmlFor={`${uid}-residency`} hint="where compute may run"
          help={
            <>
              Independent of protection (step 3). <code className="font-mono">onprem_only</code> is
              absolute: a cloud provider is filtered out of the candidate pool before the router
              scores anything, whatever the protection posture says. Nothing in step 3 ever widens
              providers, clears onprem_only, or moves where a request runs.
            </>
          }>
          <select id={`${uid}-residency`} value={p.residency ?? ""}
            onChange={(e) => setP({ ...p, residency: e.target.value || null })} className={selectCls}>
            <option value="">(app default — {app.default_residency})</option>
            {RESIDENCIES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </Row>

        <Row label="providers" hint="none = all">
          <span role="group" aria-label="allowed providers" className="flex flex-wrap gap-2">
            {PROVIDERS.map((t) => {
              const on = (p.allowed_providers ?? []).includes(t);
              return (
                <button key={t} type="button" aria-pressed={on} className={chipCls(on)}
                  onClick={() => setP({ ...p, allowed_providers: multiToggle(p.allowed_providers, t) })}>{t}</button>
              );
            })}
          </span>
        </Row>

        <Row label="tiers" hint="none = all">
          <span role="group" aria-label="allowed tiers" className="flex flex-wrap gap-2">
            {TIERS.map((t) => {
              const on = (p.allowed_tiers ?? []).includes(t);
              return (
                <button key={t} type="button" aria-pressed={on} className={chipCls(on)}
                  onClick={() => setP({ ...p, allowed_tiers: multiToggle(p.allowed_tiers, t) })}>{t}</button>
              );
            })}
          </span>
        </Row>

        <Row label="models" htmlFor={`${uid}-models`} hint="comma-separated slugs; empty = all">
          <input id={`${uid}-models`} value={modelsText} onChange={(e) => setModelsText(e.target.value)}
            placeholder="local-8b, nvidia/… (empty = all)" className={`${inputCls} min-w-64 flex-1`} />
        </Row>

        {saveFor(2)}
      </Step>

      {/* --- 3 PROTECTION ------------------------------------------------- */}
      <Step n={3} title="Protection" summary={protectionSummary(p, effPolicyMode, effPii)} dirty={dirty[3]}
        open={open.has(3)} onToggle={() => toggle(3)}>
        <Row label="policy mode" htmlFor={`${uid}-policy-mode`} hint="what the gate does to the data"
          help={
            <>
              <code className="font-mono">enforced</code> = detection, redaction, escalation, capture
              ceiling. This is the coarse switch; the Detection &amp; narrowing group below it refines
              the enforced posture. “(global default)” hands the app back to the deployment's posture —
              a real change, and one the server refuses if it would strand raw eval capture on a
              now-enforced onprem_only app. Separate from placement: changing it never widens
              providers, never clears onprem_only, and never moves where a request runs.
              {passthroughCeiling && (
                <>
                  {" "}This deployment sets <code className="font-mono">policy.allow_app_passthrough:
                  false</code>, so a save that resolves to passthrough is refused with that reason.
                </>
              )}
            </>
          }>
          <select id={`${uid}-policy-mode`} value={p.policy_mode ?? ""}
            onChange={(e) => setP({ ...p, policy_mode: e.target.value || null })}
            className={`${selectCls} ${passthrough ? "border-frontier text-frontier" : ""}`}>
            <option value="">
              (global default{deployment ? ` — ${deployment.default_policy_mode}` : ""})
            </option>
            {POLICY_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </Row>

        {/* The consequence is stated where the operator picks it, not in a
            tooltip. Owner voice, and only what is actually true: passthrough
            removes protection and touches nothing else. */}
        {passthrough && (
          <div className="rounded-md border border-frontier bg-frontier/10 text-frontier px-3 py-2 text-xs flex items-start gap-2">
            <span aria-hidden>⚠</span>
            <span>
              <strong>Protection is off for this application.</strong> Nothing is detected and
              nothing is redacted, in either direction — the prompt reaches the model verbatim
              and the completion comes back verbatim. Custom recognizers and sensitivity rules
              do not run.
              <br />
              What continues unchanged: routing and tier selection, budgets, rate limits,
              usage metering, the full decision trace, and Langfuse tracing.
              <br />
              <strong>Placement is untouched.</strong> Residency in step 2 still decides where compute
              may run — <code className="font-mono">onprem_only</code> stays absolute, and
              onprem_only + passthrough is a valid, honest configuration.
              <br />
              This posture is never silent: it is folded into{" "}
              <code className="font-mono">app_policy_version</code>, stamped on every response and
              emitted on the trace. A deployment can forbid it outright
              (<code className="font-mono">policy.allow_app_passthrough</code>), in which case the
              save is refused with that reason.
            </span>
          </div>
        )}

        {/* The three narrowing knobs + the detector choice are refinements
            WITHIN the enforced posture. In passthrough nothing is detected, so
            they do nothing — collapsed to one line and labelled, but deliberately
            still editable and never cleared: a posture toggle must not destroy
            the operator's stored configuration. */}
        <SubGroup
          title="Detection & narrowing"
          summary={detectionSummary}
          tone={passthrough ? "inert" : "normal"}
          open={detectionOpen}
          onToggle={() => setDetectionOverride(!detectionOpen)}>
          {passthrough && (
            <p className="text-[10px] text-frontier">
              Inert while <code className="font-mono">policy_mode = passthrough</code> — nothing is
              detected, so there is nothing to narrow. These values are kept, not cleared, and apply
              again on enforced.
            </p>
          )}

          <Row label="pii detection" htmlFor={`${uid}-pii`} hint="unset inherits the deployment's detector"
            help={
              <>
                Unset inherits the deployment's live detector — that is not a downgrade. Only an
                explicit <code className="font-mono">off</code>/<code className="font-mono">regex</code> is,
                and it blinds the gate to everything. Prefer the three narrow levers below.
                {piiDowngradeCeiling && (
                  <>
                    {" "}This deployment sets <code className="font-mono">policy.allow_app_pii_downgrade:
                    false</code>, so off/regex is refused with that reason.
                  </>
                )}
              </>
            }>
            <select id={`${uid}-pii`} value={p.pii_detection ?? ""}
              onChange={(e) => setP({ ...p, pii_detection: e.target.value || null })} className={selectCls}>
              <option value="">
                (global default{deployment ? ` — ${deployment.default_pii_detection}` : ""})
              </option>
              {PII_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </Row>

          <Row label="min confidence" htmlFor={`${uid}-score`} hint="0.0–1.0; blank = no floor"
            help={
              <>
                Detected entities scoring below this are dropped before redaction (many false
                positives score ~0.6). Never applies to US_SSN, and never drops regex or
                custom-recognizer hits (they carry no score). Blank the box to remove a stored floor
                entirely; <code className="font-mono">0</code> is different — it is a floor that
                passes every score.
              </>
            }>
            <input id={`${uid}-score`} type="number" min={0} max={1} step="0.05"
              value={p.pii_score_threshold ?? ""} placeholder="none"
              onChange={(e) => setP({
                ...p,
                pii_score_threshold: e.target.value === "" ? null : Number(e.target.value),
              })}
              className={`${inputCls} w-20`} />
          </Row>

          <Row label="exempt types" align="start" hint={`${denylist.length} selected`}
            help={
              <>
                Entity types the gate still detects but does not redact for this app — the
                surgical alternative to turning detection off. Presidio built-ins only; an
                unknown name is refused. US_SSN can never be exempted.
              </>
            }>
            <span role="group" aria-label="exempt entity types" className="flex min-w-64 flex-1 flex-wrap gap-1">
              {BUILTIN_ENTITIES.map((ent) => {
                const locked = PROTECTED_ENTITIES.includes(ent);
                const on = denylist.includes(ent);
                return (
                  <button key={ent} type="button"
                    disabled={locked}
                    aria-pressed={on}
                    title={locked
                      ? "US_SSN carries an absolute redaction guarantee and can never be exempted"
                      : undefined}
                    className={`${chipCls(on)} ${locked ? "opacity-40 cursor-not-allowed" : ""}`}
                    onClick={() => setP({ ...p, pii_entity_denylist: clearableToggle(p.pii_entity_denylist, ent) })}>
                    {locked ? `${ent} (locked)` : ent}
                  </button>
                );
              })}
            </span>
          </Row>

          <Row label="exempt values" align="start"
            hint={`${exemptPatterns.length}/${MAX_EXEMPT_PATTERNS} patterns · max ${MAX_EXEMPT_PATTERN_LEN} chars each`}
            help={
              <>
                Regexes matched against the whole detected value, whatever type Presidio gave it —
                the type-independent complement to exempt types, for a value shape that keeps
                landing under a different label.
              </>
            }>
            <div className="flex min-w-64 flex-1 flex-col gap-1.5">
              {exemptPatterns.map((pat, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input value={pat} onChange={(e) => setPattern(i, e.target.value)}
                    aria-label={`exempt value pattern ${i + 1}`}
                    placeholder="regex, e.g. \d{2} \d{4} \d{3}[A-Z]"
                    maxLength={MAX_EXEMPT_PATTERN_LEN}
                    className={`${inputCls} flex-1 min-w-[10rem] ${patternErrors[i] ? "border-frontier" : ""}`} />
                  <button type="button"
                    aria-label={`Delete exempt value pattern ${i + 1}`}
                    onClick={() => setP({
                      ...p,
                      pii_exempt_patterns: exemptPatterns.filter((_, idx) => idx !== i),
                    })}
                    className="cursor-pointer rounded text-xs text-frontier hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
                    delete
                  </button>
                  {patternErrors[i] && <span className="text-[10px] text-frontier">{patternErrors[i]}</span>}
                </div>
              ))}
              <button type="button"
                disabled={exemptPatterns.length >= MAX_EXEMPT_PATTERNS}
                onClick={() => setP({ ...p, pii_exempt_patterns: [...exemptPatterns, ""] })}
                className={`${linkBtnCls} self-start`}>
                + add pattern
              </button>
              {exemptPatterns.some(Boolean) && <ExemptPatternTester patterns={exemptPatterns} />}
            </div>
          </Row>
        </SubGroup>

        {saveFor(3)}
      </Step>

      {/* --- 4 TELEMETRY -------------------------------------------------- */}
      {/* Langfuse credentials and capture_io are ONE step because one gates the
          other: `full` is refused without this app's own project. They used to
          sit ~1000px apart with the dependency invisible. */}
      <Step n={4} title="Telemetry" summary={telemetrySummary(p, app, effCapture)} dirty={dirty[4]}
        open={open.has(4)} onToggle={() => toggle(4)}>
        <LangfuseCredsForm app={app} onSaved={onSaved} />

        <Row label="eval capture" htmlFor={`${uid}-capture`}
          hint="raw (full) goes only to this app's own project"
          help={
            <>
              What this app's traces carry. Raw I/O only ever reaches this app's own Langfuse
              project — never the shared one, which fails closed to{" "}
              <code className="font-mono">redacted</code> if the project is missing. An onprem_only
              app must first declare <code className="font-mono">policy_mode = passthrough</code>{" "}
              (step 3) before <code className="font-mono">full</code> is permitted: residency is
              placement, policy_mode is protection, and it is the protection posture the capture
              ceiling keys on.
            </>
          }>
          <select id={`${uid}-capture`} value={p.capture_io ?? ""}
            onChange={(e) => setP({ ...p, capture_io: e.target.value || null })} className={selectCls}>
            <option value="">
              (global default{deployment ? ` — ${deployment.default_capture_io}` : ""})
            </option>
            <option value="off">off</option>
            {/* Never disable the currently-selected value — the operator must
                always be able to see what the app is actually set to. */}
            <option value="redacted" disabled={!!redactedBlocked && p.capture_io !== "redacted"}>
              redacted{redactedBlocked ? ` — ${redactedBlocked}` : ""}
            </option>
            <option value="full" disabled={!!fullBlocked && p.capture_io !== "full"}>
              full{fullBlocked ? ` — ${fullBlocked}` : ""}
            </option>
          </select>
        </Row>

        {saveFor(4)}
      </Step>

      {/* --- 5 DELIVERY AND LIMITS ---------------------------------------- */}
      <Step n={5} title="Delivery and limits" summary={deliverySummary(p, deployment)} dirty={dirty[5]}
        open={open.has(5)} onToggle={() => toggle(5)}>
        <Row label="streaming" htmlFor={`${uid}-stream`} hint="step 9 verifies the whole response"
          help={
            <>
              <code className="font-mono">verified_then_stream</code> chunks the finished, verified
              answer (guarantee intact, no first-token win); <code className="font-mono">stream_unverified</code>{" "}
              waives step 9; <code className="font-mono">incremental_verify</code> streams through a
              hold-back redactor but cannot enforce a whole-response verdict.
              <br />
              The floor is absolute and independent of this setting: an onprem_only or
              require_verification request falls back to verified_then_stream whatever is chosen here.
            </>
          }>
          <select id={`${uid}-stream`} value={p.stream_mode ?? ""}
            onChange={(e) => setP({ ...p, stream_mode: e.target.value || null })} className={selectCls}>
            {/* stream_mode has no global default server-side: unset simply
                means this app never opted in, so `stream: true` is rejected
                under server.reject_stream_requests. */}
            <option value="">
              (unset — {deployment && !deployment.reject_stream_requests
                ? "stream: true served unstreamed"
                : "stream: true rejected"})
            </option>
            {STREAM_MODES.map((m) => (
              <option key={m} value={m}
                disabled={m === "stream_unverified" && !!streamUnverifiedBlocked && p.stream_mode !== m}>
                {m}{m === "stream_unverified" && streamUnverifiedBlocked ? ` — ${streamUnverifiedBlocked}` : ""}
              </option>
            ))}
          </select>
        </Row>

        <Row label="Headroom" htmlFor={`${uid}-headroom`} hint="per-app context compression; safety bypasses still apply">
          <select id={`${uid}-headroom`}
            value={p.compression_enabled === null ? "" : p.compression_enabled ? "on" : "off"}
            onChange={(e) => setP({
              ...p,
              compression_enabled: e.target.value === "" ? null : e.target.value === "on",
            })}
            className={selectCls}>
            <option value="">(global default)</option>
            <option value="on">on</option>
            <option value="off">off</option>
          </select>
        </Row>

        <Row label="max rpm" htmlFor={`${uid}-rpm`} hint="requests per minute; blank = no rate limit">
          <input id={`${uid}-rpm`} type="number" min={1} value={p.max_rpm ?? ""}
            onChange={(e) => setP({ ...p, max_rpm: e.target.value ? Number(e.target.value) : null })}
            className={`${inputCls} w-20`} />
        </Row>

        <Row label="budget $/day" htmlFor={`${uid}-budget`} hint="daily spend cap; blank = uncapped">
          <input id={`${uid}-budget`} type="number" min={0.01} step="0.01" value={p.max_spend_usd_per_day ?? ""}
            onChange={(e) => setP({ ...p, max_spend_usd_per_day: e.target.value ? Number(e.target.value) : null })}
            className={`${inputCls} w-24`} />
        </Row>

        <Row label="cost cap $/request" htmlFor={`${uid}-reqcap`}
          hint="estimated cost cap per request; blank = deployment default (agent clients need more)">
          <input id={`${uid}-reqcap`} type="number" min={0.01} step="0.01" value={p.max_cost_usd_per_request ?? ""}
            onChange={(e) => setP({ ...p, max_cost_usd_per_request: e.target.value ? Number(e.target.value) : null })}
            className={`${inputCls} w-24`} />
        </Row>

        {saveFor(5)}
      </Step>

      {/* --- 6 PRIVACY RULES ---------------------------------------------- */}
      <Step n={6} title="Privacy rules" summary={privacy.summary} advanced
        open={open.has(6)} onToggle={() => toggle(6)}>
        <p className={hintCls}>
          Custom recognizers and sensitivity rules that override the deployment defaults for this
          application. Saved separately, with its own button below.
          {passthrough && (
            <span className="text-frontier"> None of it runs while policy_mode = passthrough.</span>
          )}
        </p>
        {(open.has(6) || privacy.mounted) && <PrivacyRulesEditor appId={app.id} />}
      </Step>
    </div>
  );
}
