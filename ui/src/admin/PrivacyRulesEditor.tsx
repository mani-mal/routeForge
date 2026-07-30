// Per-app privacy rules editor: custom PII recognizers + an ordered list of
// sensitivity rules that override the global policy.yaml defaults for one
// application. Mirrors PolicyEditor's chip/select idiom (ApplicationsPanel.tsx)
// so the Settings tab reads as one system. Backend: app/api/admin_routes.py,
// GET/PUT /admin/applications/{id}/privacy (Task 10).
//
// This UI never renders raw request data — it only edits rule *configuration*.
// The optional "test against sample text" box below runs client-side only,
// over text the admin types themselves; nothing is sent to the server.
import { useEffect, useState } from "react";
import { getPrivacy, putPrivacy } from "../api";
import type { PrivacyProfile, Recognizer, SensitivityRule } from "../api";

const SENSITIVITY = ["public", "internal", "confidential", "restricted"];
const RISK_CLASS = ["low", "medium", "high"];
const RESIDENCY = ["any", "private_cloud_ok", "onprem_only"];

// Presidio built-in entity types (app/policy/recognizers.py PRESIDIO_BUILTINS).
// Custom recognizer names (defined below, per-app) are appended to this list
// when building the entities multi-select.
const BUILTIN_ENTITIES = [
  "US_SSN", "CREDIT_CARD", "EMAIL_ADDRESS", "PHONE_NUMBER", "US_BANK_NUMBER",
  "PERSON", "LOCATION", "DATE_TIME", "IP_ADDRESS", "URL", "MEDICAL_LICENSE",
  "US_DRIVER_LICENSE", "US_PASSPORT", "IBAN_CODE", "NRP", "US_ITIN",
];

// Entities that plausibly carry sensitive personal data. Used only to flag a
// rule that maps one of these to residency "any" — i.e. cloud-eligible — so
// that deliberate downgrade is surfaced, not silently saved.
const SENSITIVE_LOOKING = new Set([
  "US_SSN", "CREDIT_CARD", "US_BANK_NUMBER", "MEDICAL_LICENSE", "PERSON",
  "EMAIL_ADDRESS", "PHONE_NUMBER",
]);

const AMBER = "#f59e0b";

function emptyRule(): SensitivityRule {
  return { entities: [], sensitivity: "internal", risk_class: "low", residency: "any", require_verification: false };
}
function emptyRecognizer(): Recognizer {
  return { name: "", regex: "", score: 0.85 };
}
function ruleIsDowngrade(rule: SensitivityRule): boolean {
  return rule.residency === "any" && rule.entities.some((e) => SENSITIVE_LOOKING.has(e));
}
function toggleEntity(entities: string[], value: string): string[] {
  return entities.includes(value) ? entities.filter((e) => e !== value) : [...entities, value];
}

// Client-side preview only: runs each recognizer's regex over admin-entered
// text and highlights matches. The server remains the source of truth for
// what actually gets redacted.
function TestBox({ recognizers }: { recognizers: Recognizer[] }) {
  const [text, setText] = useState("");

  type Match = { name: string; start: number; end: number };
  const matches: Match[] = [];
  for (const r of recognizers) {
    if (!r.regex) continue;
    try {
      const re = new RegExp(r.regex, "g");
      let m: RegExpExecArray | null;
      while ((m = re.exec(text)) !== null) {
        if (m[0].length === 0) {
          re.lastIndex += 1; // avoid an infinite loop on a zero-width match
          continue;
        }
        matches.push({ name: r.name || "(unnamed)", start: m.index, end: m.index + m[0].length });
      }
    } catch {
      /* invalid regex (e.g. Python-only syntax) — skip in the client preview */
    }
  }
  matches.sort((a, b) => a.start - b.start);

  const parts: { text: string; name?: string }[] = [];
  let cursor = 0;
  for (const m of matches) {
    if (m.start < cursor) continue; // drop overlaps, keep the earlier match
    if (m.start > cursor) parts.push({ text: text.slice(cursor, m.start) });
    parts.push({ text: text.slice(m.start, m.end), name: m.name });
    cursor = m.end;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) });

  return (
    <div className="flex flex-col gap-2">
      <p className="text-[10px] text-gray-600">
        Runs your recognizer regexes over text you type here, in your browser only. Nothing is sent anywhere.
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Type or paste sample text to preview recognizer matches…"
        rows={3}
        className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100 w-full font-mono"
      />
      {text && (
        <div className="rounded bg-ink/60 border border-edge px-2 py-1.5 text-sm text-gray-300 whitespace-pre-wrap break-words">
          {parts.map((p, i) =>
            p.name ? (
              <mark key={i} title={p.name} className="rounded px-0.5 bg-accent/30 text-accent">
                {p.text}
              </mark>
            ) : (
              <span key={i}>{p.text}</span>
            ),
          )}
        </div>
      )}
    </div>
  );
}

export function PrivacyRulesEditor({ appId }: { appId: string }) {
  const [profile, setProfile] = useState<PrivacyProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function load() {
    setLoading(true);
    setErr(null);
    setSaved(false);
    try {
      setProfile(await getPrivacy(appId));
    } catch {
      setErr("Could not load privacy rules.");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId]);

  async function save(next: PrivacyProfile) {
    setBusy(true);
    setErr(null);
    setSaved(false);
    try {
      // The PUT handler only returns {"ok": true} — it does not echo the
      // stored profile. Re-fetch so `profile` always reflects what the
      // server actually persisted (and, on Reset, the global-fallback rules
      // it now serves), never the raw `{ok: true}` response body.
      await putPrivacy(appId, next);
      const fresh = await getPrivacy(appId);
      setProfile(fresh);
      setSaved(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="text-sm text-gray-500">Loading privacy rules…</p>;
  if (!profile) return <p className="text-sm text-frontier">{err ?? "Could not load privacy rules."}</p>;

  // Defensive: normalize to arrays here so a future shape surprise from the
  // server (or the {ok:true} response, if a future refactor reintroduces
  // that mistake) degrades gracefully instead of crashing the render.
  const recognizers = profile.recognizers ?? [];
  const rules = profile.rules ?? [];
  const customNames = recognizers.map((r) => r.name).filter(Boolean);
  const entityOptions = [...BUILTIN_ENTITIES, ...customNames.filter((n) => !BUILTIN_ENTITIES.includes(n))];
  const hasDowngrade = rules.some(ruleIsDowngrade);

  function updateRecognizer(i: number, patch: Partial<Recognizer>) {
    const next = recognizers.map((r, idx) => (idx === i ? { ...r, ...patch } : r));
    setProfile({ ...profile!, recognizers: next });
  }
  function addRecognizer() {
    setProfile({ ...profile!, recognizers: [...recognizers, emptyRecognizer()] });
  }
  function deleteRecognizer(i: number) {
    setProfile({ ...profile!, recognizers: recognizers.filter((_, idx) => idx !== i) });
  }

  function updateRule(i: number, patch: Partial<SensitivityRule>) {
    const next = rules.map((r, idx) => (idx === i ? { ...r, ...patch } : r));
    setProfile({ ...profile!, rules: next });
  }
  function addRule() {
    setProfile({ ...profile!, rules: [...rules, emptyRule()] });
  }
  function deleteRule(i: number) {
    setProfile({ ...profile!, rules: rules.filter((_, idx) => idx !== i) });
  }
  function moveRule(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 0 || j >= rules.length) return;
    const next = [...rules];
    [next[i], next[j]] = [next[j], next[i]];
    setProfile({ ...profile!, rules: next });
  }

  async function resetToGlobal() {
    await save({ recognizers: [], rules: [] });
  }

  const chip = (on: boolean) =>
    `px-2 py-0.5 rounded text-xs border ${on ? "border-accent text-accent bg-accent/10" : "border-edge text-gray-500"}`;

  return (
    <div className="flex flex-col gap-4 text-sm">
      {hasDowngrade && (
        <div
          className="rounded-md border px-3 py-2 text-xs flex items-start gap-2"
          style={{ borderColor: AMBER, backgroundColor: `${AMBER}1a`, color: AMBER }}
        >
          <span aria-hidden>⚠</span>
          <span>
            This routes potentially sensitive data off-prem — a rule below matches a sensitive-looking entity
            (SSN, card, bank number, medical license, name, email, or phone) but allows residency &quot;any&quot;.
          </span>
        </div>
      )}

      <section>
        <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">Custom recognizers</div>
        <div className="flex flex-col gap-1.5">
          {recognizers.map((r, i) => (
            <div key={i} className="flex items-center gap-2 flex-wrap">
              <input value={r.name} onChange={(e) => updateRecognizer(i, { name: e.target.value })}
                placeholder="name"
                className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100 w-28" />
              <input value={r.regex} onChange={(e) => updateRecognizer(i, { regex: e.target.value })}
                placeholder="regex"
                className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100 font-mono flex-1 min-w-[10rem]" />
              <input type="number" min={0} max={1} step="0.05" value={r.score} title="score"
                onChange={(e) => updateRecognizer(i, { score: Number(e.target.value) })}
                className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100 w-16" />
              <button onClick={() => deleteRecognizer(i)} className="text-xs text-frontier hover:underline">delete</button>
            </div>
          ))}
          {recognizers.length === 0 && (
            <p className="text-xs text-gray-600">No custom recognizers — using global defaults only.</p>
          )}
        </div>
        <button onClick={addRecognizer} className="mt-2 text-xs text-accent hover:underline">+ add recognizer</button>
      </section>

      <section>
        <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">Sensitivity rules</div>
        <p className="text-[10px] text-gray-600 mb-2">Evaluated top to bottom — the first matching rule wins.</p>
        <div className="flex flex-col gap-3">
          {rules.map((rule, i) => {
            const downgrade = ruleIsDowngrade(rule);
            return (
              <div key={i}
                className={`rounded-md border p-2 flex flex-col gap-2 ${downgrade ? "" : "border-edge"}`}
                style={downgrade ? { borderColor: AMBER } : undefined}>
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">#{i + 1}</span>
                  <div className="flex items-center gap-1">
                    <button onClick={() => moveRule(i, -1)} disabled={i === 0}
                      className="text-xs text-gray-400 hover:text-gray-100 disabled:opacity-30">↑</button>
                    <button onClick={() => moveRule(i, 1)} disabled={i === rules.length - 1}
                      className="text-xs text-gray-400 hover:text-gray-100 disabled:opacity-30">↓</button>
                    <button onClick={() => deleteRule(i)} className="text-xs text-frontier hover:underline ml-2">delete</button>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1">
                  {entityOptions.map((ent) => (
                    <button key={ent} className={chip(rule.entities.includes(ent))}
                      onClick={() => updateRule(i, { entities: toggleEntity(rule.entities, ent) })}>
                      {ent}
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-3 flex-wrap">
                  <label className="flex items-center gap-1 text-xs text-gray-500">
                    sensitivity
                    <select value={rule.sensitivity} onChange={(e) => updateRule(i, { sensitivity: e.target.value })}
                      className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100">
                      {SENSITIVITY.map((s) => <option key={s} value={s}>{s}</option>)}
                    </select>
                  </label>
                  <label className="flex items-center gap-1 text-xs text-gray-500">
                    risk
                    <select value={rule.risk_class} onChange={(e) => updateRule(i, { risk_class: e.target.value })}
                      className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100">
                      {RISK_CLASS.map((s) => <option key={s} value={s}>{s}</option>)}
                    </select>
                  </label>
                  <label className="flex items-center gap-1 text-xs text-gray-500">
                    residency
                    <select value={rule.residency} onChange={(e) => updateRule(i, { residency: e.target.value })}
                      className="rounded bg-ink border border-edge px-2 py-1 text-xs text-gray-100">
                      {RESIDENCY.map((s) => <option key={s} value={s}>{s}</option>)}
                    </select>
                  </label>
                  <label className="flex items-center gap-1.5 text-xs text-gray-500">
                    <input type="checkbox" checked={rule.require_verification}
                      onChange={(e) => updateRule(i, { require_verification: e.target.checked })} />
                    require verification
                  </label>
                </div>
              </div>
            );
          })}
          {rules.length === 0 && (
            <p className="text-xs text-gray-600">No app-specific rules — using global defaults only.</p>
          )}
        </div>
        <button onClick={addRule} className="mt-2 text-xs text-accent hover:underline">+ add rule</button>
      </section>

      <section>
        <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">Test against sample text</div>
        <TestBox recognizers={recognizers} />
      </section>

      <div className="flex items-center gap-3">
        <button onClick={() => save({ recognizers, rules })} disabled={busy}
          className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1.5 text-xs disabled:opacity-50">
          {busy ? "saving…" : "Save privacy rules"}
        </button>
        <button onClick={resetToGlobal} disabled={busy}
          className="rounded-md border border-edge text-gray-400 hover:border-gray-600 px-3 py-1.5 text-xs disabled:opacity-50">
          Reset to global defaults
        </button>
        {saved && !err && <span className="text-xs text-cheap">saved</span>}
        {err && <span className="text-xs text-frontier">{err}</span>}
      </div>
    </div>
  );
}
