// Applications management: list, create (with one-time token), tokens, policy.
import { useEffect, useState } from "react";
import { adminApi } from "../api";
import type { AdminApp, AdminPolicy } from "../api";
import { Panel } from "../components/common";
import { BUILTIN_ENTITIES, PrivacyRulesEditor } from "./PrivacyRulesEditor";

const TIERS = ["cheap", "mid", "frontier"];
const PROVIDERS = ["onprem", "azure", "bedrock"];
const RESIDENCIES = ["any", "private_cloud_ok", "onprem_only"];
// Per-app override vocabularies — mirror app/policy/enums.py exactly
// (PII_MODES / STREAM_MODES / POLICY_MODES). Exported so the protection and
// streaming controls can reuse them without respelling the values.
export const PII_MODES = ["off", "regex", "presidio"];
export const STREAM_MODES = ["off", "verified_then_stream", "stream_unverified", "incremental_verify"];
export const POLICY_MODES = ["enforced", "passthrough"];

// Copy-to-clipboard that actually works on this deployment.
//
// `navigator.clipboard` is only defined in a SECURE CONTEXT. The console is
// routinely served over plain http on a NodePort IP (the DGX lab reaches it at
// http://<node>:30808), where the whole API is undefined — so a button wired
// straight to it silently does nothing in exactly the environment operators
// use. Falls back to the execCommand path, and reports honestly when neither
// works rather than showing a "copied" tick that didn't happen.
function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).then(() => true, () => execCopy(text));
  }
  return Promise.resolve(execCopy(text));
}

function execCopy(text: string): boolean {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Keep it off-screen and unfocusable-looking, but still selectable.
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

// The application id is the value every admin API call, config script and
// support conversation is keyed by (ui-open-items #7: a real operator asked
// "where's the app id?" while trying to set a per-app policy over curl). It
// used to render as an unlabelled grey parenthetical after the name.
function CopyButton({ value, what }: { value: string; what: string }) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");
  return (
    <button
      type="button"
      title={`Copy ${what} to clipboard`}
      aria-label={`Copy ${what}`}
      onClick={async () => {
        setState((await copyText(value)) ? "ok" : "fail");
        window.setTimeout(() => setState("idle"), 1500);
      }}
      className="rounded border border-edge px-1 py-0.5 text-[10px] text-gray-400 hover:text-gray-100 hover:border-gray-500">
      {state === "ok" ? "copied" : state === "fail" ? "copy failed" : "copy"}
    </button>
  );
}

function CopyableId({ value, label = "Application ID" }: { value: string; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-wide text-gray-500">{label}</span>
      <code className="font-mono text-xs text-gray-200 select-all">{value}</code>
      <CopyButton value={value} what={label.toLowerCase()} />
    </span>
  );
}

// A token shown exactly once, right after it's minted.
function TokenReveal({ token }: { token: string }) {
  return (
    <div className="rounded-md border border-accent/60 bg-accent/10 p-3 text-sm">
      <div className="text-accent font-medium mb-1">New token — copy it now, it won't be shown again</div>
      <code className="block break-all font-mono text-gray-100">{token}</code>
      <div className="mt-2"><CopyButton value={token} what="token" /></div>
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

// Same toggle, but it never collapses to null: an emptied list is sent as `[]`,
// which stores "nothing exempt" explicitly. (Since `reset`, null would also
// clear the stored list rather than silently re-applying it — but `[]` says
// what it means for a list whose empty state is a real, chosen value.)
function clearableToggle(list: string[] | null, value: string): string[] {
  const set = new Set(list ?? []);
  set.has(value) ? set.delete(value) : set.add(value);
  return [...set];
}

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

// A labelled group in the policy form. The form is grouped along the product's
// own two axes on purpose: `residency` is PLACEMENT (where compute may run),
// `policy_mode` is PROTECTION (what the gate does to the data), and they are
// independent (CLAUDE.md rule 2a). A flat stack of rows is exactly what let an
// operator read one as implying the other.
function Section({ title, blurb, children }: {
  title: string; blurb?: string; children: React.ReactNode;
}) {
  return (
    <section className="rounded-md border border-edge/60 p-2.5 flex flex-col gap-2">
      <div>
        <div className="text-[11px] font-display uppercase tracking-wide text-gray-400">{title}</div>
        {blurb && <div className="text-[10px] text-gray-600 mt-0.5">{blurb}</div>}
      </div>
      {children}
    </section>
  );
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

// Sample-value preview for pii_exempt_patterns, in the idiom of the Privacy
// tab's "Test against sample text" box — but a different test, because the
// server applies a different operation: a pattern is `re.fullmatch`ed against
// the *detected entity's value*, not searched over the prompt. So the input
// here is one candidate value per line, and the verdict per line is binary.
// `^(?:…)$` is the browser's closest approximation of Python's fullmatch.
function ExemptPatternTester({ patterns }: { patterns: string[] }) {
  const [text, setText] = useState("");
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
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="One candidate value per line, e.g. 12 3456 789A — checked in your browser only, nothing is sent anywhere."
        rows={2}
        className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100 w-full font-mono"
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
      <p className="text-[10px] text-gray-600">
        Approximates the server's <code className="font-mono">re.fullmatch</code> against the detected
        entity's value. A US_SSN match is never exempted, however this reads.
      </p>
    </div>
  );
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

function PolicyEditor({ app, onSaved }: { app: AdminApp; onSaved: () => void }) {
  const [p, setP] = useState<AdminPolicy>(
    app.policy ?? {
      allowed_tiers: null, allowed_providers: null, allowed_models: null,
      residency: null, max_spend_usd_per_day: null, max_rpm: null,
      compression_enabled: null, capture_io: null,
      pii_detection: null, stream_mode: null, policy_mode: null,
      pii_entity_denylist: null, pii_score_threshold: null, pii_exempt_patterns: null,
    });
  const [modelsText, setModelsText] = useState((p.allowed_models ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

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

  const chip = (on: boolean) =>
    `px-2 py-0.5 rounded text-xs border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
      on ? "border-accent text-accent bg-accent/10" : "border-edge text-gray-500"
    }`;

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
  // The UI can't see serving.policy.default_policy_mode, so an unset
  // policy_mode reads as `enforced` — the conservative half.
  const passthrough = (p.policy_mode ?? "enforced") === "passthrough";
  // `full` is refused for an onprem_only app only while it is still in the
  // `enforced` posture (PR #34 — residency is placement, policy_mode is
  // protection; onprem_only + passthrough + full is valid).
  const fullBlocked = !hasOwnProject
    ? "needs this app's own Langfuse project"
    : effResidency === "onprem_only" && !passthrough
      ? "onprem_only + enforced — set policy_mode = passthrough"
      : null;
  // `redacted` reuses the gate's sanitized prompt, so it is refused wherever
  // nothing is sanitized. Note pii_detection = null means "inherit the live
  // global detector", which the server does NOT treat as a downgrade — only an
  // explicit off/regex trips this.
  const redactedBlocked = passthrough
    ? "policy_mode = passthrough sanitizes nothing"
    : p.pii_detection === "off" || p.pii_detection === "regex"
      ? `pii_detection = ${p.pii_detection} sanitizes nothing`
      : null;
  // stream_unverified waives step 9 outright, which onprem_only may never do
  // (set_policy 422s on the pair; app/pipeline.py backstops it at runtime).
  const streamUnverifiedBlocked = effResidency === "onprem_only"
    ? "onprem_only can never waive verification"
    : null;

  const denylist = p.pii_entity_denylist ?? [];
  const exemptPatterns = p.pii_exempt_patterns ?? [];
  const patternErrors = exemptPatterns.map(patternError);

  function setPattern(i: number, value: string) {
    setP({ ...p, pii_exempt_patterns: exemptPatterns.map((x, idx) => (idx === i ? value : x)) });
  }

  // Stored values a save would clear (see resetList). Recomputed each render so
  // the notice under Save tracks the form live.
  const pendingReset = resetList(app.policy, payload());

  const label = "text-xs text-gray-500 w-28 shrink-0";
  const hint = "text-[10px] text-gray-600";
  const select = "rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100";

  return (
    <div className="flex flex-col gap-3 text-sm">

      {/* --- PLACEMENT --------------------------------------------------- */}
      <Section
        title="Placement — where compute may run"
        blurb="Independent of protection below. onprem_only is absolute: a cloud provider is filtered out of the candidate pool before the router scores anything, whatever the protection posture says.">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={label}>residency</span>
          <select value={p.residency ?? ""} onChange={(e) => setP({ ...p, residency: e.target.value || null })}
            className={select}>
            <option value="">(app default — {app.default_residency})</option>
            {RESIDENCIES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className={label}>providers</span>
          {PROVIDERS.map((t) => (
            <button key={t} className={chip((p.allowed_providers ?? []).includes(t))}
              onClick={() => setP({ ...p, allowed_providers: multiToggle(p.allowed_providers, t) })}>{t}</button>
          ))}
          <span className={hint}>none = all</span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className={label}>tiers</span>
          {TIERS.map((t) => (
            <button key={t} className={chip((p.allowed_tiers ?? []).includes(t))}
              onClick={() => setP({ ...p, allowed_tiers: multiToggle(p.allowed_tiers, t) })}>{t}</button>
          ))}
          <span className={hint}>none = all</span>
        </div>
        <label className="flex items-center gap-2 flex-wrap text-xs text-gray-500">
          <span className={label}>models</span>
          <input
            value={modelsText}
            onChange={(e) => setModelsText(e.target.value)}
            placeholder="local-8b, nvidia/… (empty = all)"
            className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 min-w-64 flex-1"
          />
          <span className={hint}>comma-separated slugs; empty = all</span>
        </label>
      </Section>

      {/* --- PROTECTION -------------------------------------------------- */}
      <Section
        title="Protection — what the gate does to the data"
        blurb="Separate from placement. Changing anything here never widens providers, never clears onprem_only, and never moves where a request runs.">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={label}>policy mode</span>
          <select value={p.policy_mode ?? ""} onChange={(e) => setP({ ...p, policy_mode: e.target.value || null })}
            className={`${select} ${passthrough ? "border-frontier text-frontier" : ""}`}>
            <option value="">(global default)</option>
            {POLICY_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <span className={hint}>
            enforced = detection, redaction, escalation, capture ceiling. This is the
            coarse switch; everything below it refines the enforced posture.
            “(global default)” hands the app back to the deployment's posture — a real change,
            and one the server refuses if it would strand raw eval capture on a now-enforced
            onprem_only app.
          </span>
        </div>
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
              <strong>Placement is untouched.</strong> Residency above still decides where compute
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
            they do nothing — dimmed and labelled, but deliberately still
            editable and never cleared: a posture toggle must not destroy the
            operator's stored configuration. */}
        <div className={`flex flex-col gap-2 rounded border border-edge/50 p-2 ${passthrough ? "opacity-50" : ""}`}>
          <div className="text-[10px] uppercase tracking-wide text-gray-500">
            Detection &amp; narrowing
            {passthrough && (
              <span className="ml-2 normal-case tracking-normal text-frontier">
                inert while policy_mode = passthrough — nothing is detected, so there is nothing
                to narrow. These values are kept, not cleared, and apply again on enforced.
              </span>
            )}
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <span className={label}>pii detection</span>
            <select value={p.pii_detection ?? ""} onChange={(e) => setP({ ...p, pii_detection: e.target.value || null })}
              className={select}>
              <option value="">(global default)</option>
              {PII_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            <span className={hint}>
              unset inherits the deployment's live detector — that is not a downgrade. Only an
              explicit <code className="font-mono">off</code>/<code className="font-mono">regex</code> is,
              and it blinds the gate to everything. Prefer the three narrow levers below.
            </span>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <span className={label}>min confidence</span>
            <input type="number" min={0} max={1} step="0.05" value={p.pii_score_threshold ?? ""}
              placeholder="none"
              onChange={(e) => setP({
                ...p,
                pii_score_threshold: e.target.value === "" ? null : Number(e.target.value),
              })}
              className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 w-20" />
            <span className={hint}>
              0.0–1.0. Detected entities scoring below this are dropped before redaction (many
              false positives score ~0.6). Never applies to US_SSN, and never drops regex or
              custom-recognizer hits (they carry no score). Blank the box to remove a stored floor
              entirely; <code className="font-mono">0</code> is different — it is a floor that
              passes every score.
            </span>
          </div>

          <div className="flex items-start gap-2 flex-wrap">
            <span className={`${label} pt-0.5`}>exempt types</span>
            <div className="flex flex-col gap-1 flex-1 min-w-64">
              <div className="flex flex-wrap gap-1">
                {BUILTIN_ENTITIES.map((ent) => {
                  const locked = PROTECTED_ENTITIES.includes(ent);
                  return (
                    <button key={ent}
                      disabled={locked}
                      title={locked
                        ? "US_SSN carries an absolute redaction guarantee and can never be exempted"
                        : undefined}
                      className={`${chip(denylist.includes(ent))} ${locked ? "opacity-40 cursor-not-allowed" : ""}`}
                      onClick={() => setP({ ...p, pii_entity_denylist: clearableToggle(p.pii_entity_denylist, ent) })}>
                      {locked ? `${ent} 🔒` : ent}
                    </button>
                  );
                })}
              </div>
              <span className={hint}>
                Entity types the gate still detects but does not redact for this app — the
                surgical alternative to turning detection off. Presidio built-ins only; an
                unknown name is refused. US_SSN can never be exempted.
              </span>
            </div>
          </div>

          <div className="flex items-start gap-2 flex-wrap">
            <span className={`${label} pt-0.5`}>exempt values</span>
            <div className="flex flex-col gap-1.5 flex-1 min-w-64">
              {exemptPatterns.map((pat, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input value={pat} onChange={(e) => setPattern(i, e.target.value)}
                    placeholder="regex, e.g. \d{2} \d{4} \d{3}[A-Z]"
                    maxLength={MAX_EXEMPT_PATTERN_LEN}
                    className={`rounded bg-ink border px-2 py-1 text-xs font-mono text-gray-100 flex-1 min-w-[10rem] ${
                      patternErrors[i] ? "border-frontier" : "border-edge"}`} />
                  <button
                    onClick={() => setP({
                      ...p,
                      pii_exempt_patterns: exemptPatterns.filter((_, idx) => idx !== i),
                    })}
                    className="text-xs text-frontier hover:underline">delete</button>
                  {patternErrors[i] && <span className="text-[10px] text-frontier">{patternErrors[i]}</span>}
                </div>
              ))}
              <div className="flex items-center gap-3">
                <button
                  disabled={exemptPatterns.length >= MAX_EXEMPT_PATTERNS}
                  onClick={() => setP({ ...p, pii_exempt_patterns: [...exemptPatterns, ""] })}
                  className="text-xs text-accent hover:underline disabled:opacity-40 disabled:no-underline">
                  + add pattern
                </button>
                <span className={hint}>
                  {exemptPatterns.length}/{MAX_EXEMPT_PATTERNS} patterns · max {MAX_EXEMPT_PATTERN_LEN} chars each
                </span>
              </div>
              <span className={hint}>
                Regexes matched against the whole detected value, whatever type Presidio gave it —
                the type-independent complement to exempt types, for a value shape that keeps
                landing under a different label.
              </span>
              {exemptPatterns.some(Boolean) && <ExemptPatternTester patterns={exemptPatterns} />}
            </div>
          </div>
        </div>
      </Section>

      {/* --- TELEMETRY --------------------------------------------------- */}
      <Section
        title="Telemetry"
        blurb="What this app's traces carry. Raw I/O only ever reaches this app's own Langfuse project — never the shared one.">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={label}>eval capture</span>
          <select
            value={p.capture_io ?? ""}
            onChange={(e) => setP({ ...p, capture_io: e.target.value || null })}
            className={select}>
            <option value="">(global default)</option>
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
          <span className={hint}>
            raw (full) goes only to this app's own Langfuse project, and an
            onprem_only app must first declare policy_mode = passthrough
          </span>
        </div>
      </Section>

      {/* --- DELIVERY / LIMITS ------------------------------------------- */}
      <Section title="Delivery and limits">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={label}>streaming</span>
          <select value={p.stream_mode ?? ""} onChange={(e) => setP({ ...p, stream_mode: e.target.value || null })}
            className={select}>
            {/* stream_mode has no global default server-side: unset simply
                means this app never opted in, so `stream: true` is rejected
                under server.reject_stream_requests. */}
            <option value="">(unset — stream: true rejected)</option>
            {STREAM_MODES.map((m) => (
              <option key={m} value={m}
                disabled={m === "stream_unverified" && !!streamUnverifiedBlocked && p.stream_mode !== m}>
                {m}{m === "stream_unverified" && streamUnverifiedBlocked ? ` — ${streamUnverifiedBlocked}` : ""}
              </option>
            ))}
          </select>
          <span className={hint}>
            verified_then_stream chunks the finished, verified answer (guarantee intact, no
            first-token win); stream_unverified waives step 9; incremental_verify streams through
            a hold-back redactor but cannot enforce a whole-response verdict.
          </span>
          <span className="basis-full text-[10px] text-gray-600">
            The floor is absolute and independent of this setting: an onprem_only or
            require_verification request falls back to verified_then_stream whatever is chosen here.
          </span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className={label}>Headroom</span>
          <select
            value={p.compression_enabled === null ? "" : p.compression_enabled ? "on" : "off"}
            onChange={(e) => setP({
              ...p,
              compression_enabled: e.target.value === "" ? null : e.target.value === "on",
            })}
            className={select}>
            <option value="">(global default)</option>
            <option value="on">on</option>
            <option value="off">off</option>
          </select>
          <span className={hint}>
            per-app context compression; safety bypasses still apply
          </span>
        </div>
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
        </div>
      </Section>

      <div className="flex items-center gap-3 flex-wrap">
        <button onClick={save} disabled={busy}
          className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1.5 text-xs disabled:opacity-50">
          {busy ? "saving…" : "Save policy"}
        </button>
        <span className={hint}>
          Saving merges: a control you don't touch keeps whatever this app has stored. Moving a
          control back to its “(global default)” option is the exception — that field is cleared,
          and the app inherits the deployment default again.
        </span>
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
        {/* The server's 422 detail, verbatim: it names the clamp that fired and
            what to set instead. basis-full so a full sentence gets its own line. */}
        {err && <span className="basis-full text-xs text-frontier">{err}</span>}
      </div>
    </div>
  );
}

// Manual per-app Langfuse creds (B15) — attach an existing Langfuse project by
// hand. This is the primary path on OSS Langfuse (org-scoped auto-provisioning
// needs an Enterprise license); it is also how air-gapped operators wire a
// project provisioned out of band. Once saved, `app.langfuse_project_id` is set, which is
// what PolicyEditor's `full`-capture gate checks — so `onSaved` must refetch
// the app for that gate to unlock. Fields reset after a successful save; the
// secret is write-only and is never rendered back, before or after saving.
function LangfuseCredsForm({ app, onSaved }: { app: AdminApp; onSaved: () => void }) {
  const [host, setHost] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [projectId, setProjectId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      await adminApi.setLangfuseCreds(app.id, {
        host, public_key: publicKey, secret_key: secretKey, project_id: projectId,
      });
      setHost(""); setPublicKey(""); setSecretKey(""); setProjectId("");
      onSaved();
    } catch {
      setErr("Could not save credentials.");
    } finally {
      setBusy(false);
    }
  }

  const canSave = host && publicKey && secretKey && projectId && !busy;

  return (
    <div className="flex flex-col gap-2 text-sm">
      <div className="flex flex-col gap-1 text-xs text-gray-500">
        <span>
          Send this app's traces to its own Langfuse project. Create the project in
          Langfuse first, then paste its <strong>project</strong>-scoped keys below.{" "}
          {app.has_langfuse_keys && <span className="text-cheap">credentials set ✓</span>}
        </span>
        <span className="text-gray-600">
          Use the keys from <em>Project → Settings → API Keys</em> (not organization
          keys). Leave blank to keep sharing the default project.
        </span>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs text-gray-400">
          host
          <input value={host} onChange={(e) => setHost(e.target.value)} placeholder="http://langfuse-web:3000"
            title="Server-reachable Langfuse (in-cluster, e.g. http://langfuse-web:3000) — not the browser URL"
            className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 w-48" />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-400">
          public key
          <input value={publicKey} onChange={(e) => setPublicKey(e.target.value)} placeholder="pk-lf-…"
            className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 w-36" />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-400">
          secret key
          <input type="password" value={secretKey} onChange={(e) => setSecretKey(e.target.value)}
            autoComplete="off" placeholder={app.has_langfuse_keys ? "(unchanged — enter to replace)" : "sk-lf-…"}
            className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 w-36" />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-400">
          project id
          <input value={projectId} onChange={(e) => setProjectId(e.target.value)} placeholder="cmt5…"
            title="The project's id (e.g. cmt5…) — the project settings URL, or GET {host}/api/public/projects"
            className="rounded bg-ink border border-edge px-2 py-1 text-xs font-mono text-gray-100 w-32" />
        </label>
        <button onClick={save} disabled={!canSave}
          className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1 text-xs disabled:opacity-50">
          {busy ? "saving…" : "Save credentials"}
        </button>
        {err && <span className="text-xs text-frontier">{err}</span>}
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
        <div className="text-xs font-display uppercase tracking-wide text-gray-500 mb-1">Langfuse credentials</div>
        <LangfuseCredsForm app={app} onSaved={refresh} />
      </section>

      <section>
        <div className="text-xs font-display uppercase tracking-wide text-gray-500 mb-1">Policy</div>
        {/* key: PolicyEditor seeds its form state once, from app.policy. Without
            a remount per application, selecting a different app would leave the
            previous app's values in the form — and now that a null control means
            "clear the stored value", that stale state would compute the reset
            list against the wrong app's stored policy. */}
        <PolicyEditor key={app.id} app={app} onSaved={refresh} />
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
