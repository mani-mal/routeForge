# Console UI — open items / backlog

Deferred UI work surfaced while walking the per-application console. None are
blocking; listed newest-concern first. See `docs/help/ui-guide.md` for how each
region currently behaves.

---

## 1. Redesign the "What happened" summary band  ← current focus

**Where:** request drill-down, top band (`ui/src/console/RequestDrilldown.tsx` →
`PlainSummary`).

**Today** it renders one run-on sentence, e.g.:

> Checked for sensitive data (none) → eligible for cloud → answered by
> Qwen2.5-7B-Instruct (local) → verified clean, in 2888 ms for $0.0000.

**Why it's unclear (the problem to solve):**

- **Four unrelated ideas are mashed into one arrow-chain** — a privacy check, a
  residency verdict, the chosen model, and an output-verification result — plus latency
  and cost tacked on the end. There's no visual hierarchy; it reads as one grey run-on
  line, so nothing stands out as *the* answer to "was this private? which model? what did
  it cost?"
- **"eligible for cloud" then "answered by … (local)" looks contradictory.** A reader
  asks "if cloud was allowed, why did it run locally?" The band never says the router
  *chose* local on cost grounds — the routing rationale is invisible here.
- **"$0.0000" reads as free.** It's really "below $0.00005, rounds to zero at 4 dp" — a
  display artifact, not a claim. (Related to item 4.)
- **Double-negative / jargon:** "sensitive data (none)", "verified clean" are
  system-voice, not owner-voice; the parenthetical "(none)" is awkward.

**Direction (not final — for whoever picks this up):**

- Replace the sentence with a small set of **labelled outcome rows/chips**, each with an
  icon and a one-line *why*:
  - **Privacy** — "No PII detected · cloud-eligible" *or* "PII detected · kept on-prem
    (residency)".
  - **Model** — name + tier + the reason it won ("cheapest tier that met the risk bar"),
    so cloud-eligible-but-served-local stops being surprising.
  - **Verification** — "clean" / "flagged for review" / "not checked".
  - **Cost** — human-readable sub-cent (see item 4).
  - **Latency** — with the multi-step 0 ms caveat resolved (item 3).
- Keep it **owner-voice and scannable**; the full technical trace stays in band 2 below
  it. Honesty rules unchanged: entity **types** only, no raw PII, no CCR contents.
- Use the `frontend-design` skill and the `demo-ui` skill constraints when building.

---

## 2. No Headroom (compression) toggle in the console

The per-app console dropped the old on/off control. Backend supports a **live** toggle
(`POST /api/controls`, no restart) but nothing in the UI calls it. Natural home: a toggle
in the app **Settings** tab or a global control in the console header. Full detail +
enable-it-meanwhile instructions in `docs/help/ui-guide.md` FAQ.

## 3. Multi-step requests report 0 ms latency

Agent/multi-step (`N hops`) requests show `0 ms` everywhere (request row, "What happened"
band, Traffic p50/p95). Per-hop latency isn't summed into the request-level total.
Locus: `app/agent/internal_endpoint.py` / `app/agent/runtime.py` (per-hop timing) and
`app/pipeline.py` (request `latency_ms`). Fix = time each hop, accumulate into the total.

## 4. Per-request cost shows "$0.0000" for cheap requests

4-dp formatting (`formatUsd`) rounds sub-$0.00005 requests to `$0.0000`, which reads as
"free". Consider a sub-cent unit ("0.003¢") or more precision on the **per-request** line
while keeping 4 dp on aggregate totals.

## 5. Two blues collide

Mid-tier dot `#3b82f6` and the routing-savings lever `#38bdf8` are both blue but mean
unrelated things (a tier vs a savings lever). They never share a panel, but recolour one
(e.g. routing → teal/violet) so they never read as related on a projector.
