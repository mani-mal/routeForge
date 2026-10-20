# Should RouteForge use a "System One" decision model (Jev / AutoJev), and how?

**Date:** 2026-09-24 · **Status:** decision memo. Nothing is built. The owner is waiting for a
good open model to become available.
**Question:** Is it a good idea for RouteForge to rely on a probabilistic decision model, and if
so, where in the pipeline and with what guard-rails?

---

## 1. Short answer

**Yes, but only where a wrong or wobbly answer cannot break a guarantee.** The best first use
is as the **label source for P2b-3 quality calibration**: a sampled judge whose output is a
number in `outcome_feedback`. That fills the gap P2b-3 left open, which is that voluntary
feedback is too sparse to calibrate from.

It is **never** a good idea as the thing that decides:
- eligibility;
- residency or `onprem_only`;
- the PII gate;
- fail-closed behaviour.

Those decisions must stay rule-based and reproducible. A probabilistic model may only make
them *stricter*, never looser.

Using it as a live router input (a semantic tier classifier) is a reasonable **second** step,
but only after the labeler has shown on your own traffic that its judgements can be trusted.

---

## 2. What "non-deterministic" actually means here

It helps to separate three different things, because only one of them is a real concern.

| Concern | Does it apply to a System One model? | Why it matters for RouteForge |
|---|---|---|
| **Sampling randomness** (the same prompt gives different text each run) | **No.** The model scores fixed options in one forward pass. It samples nothing, so there is no temperature. | This is the non-determinism people usually mean with LLMs, and it is absent here. |
| **Numeric jitter** (probabilities differ in the 3rd–5th decimal between runs because of GPU batching and kernel choice) | **Yes, a little.** | It only matters when a probability sits near a threshold, where 0.499 vs 0.501 flips the decision. It is manageable (§4). |
| **Being wrong with confidence** (a calibrated model is still wrong some of the time) | **Yes, by design.** The published claims are 84.6% accuracy with an expected calibration error (ECE) of about 0.04 on one card, and 91.9% on another. So roughly one decision in 8 to one in 12 is wrong. | This is the real issue. It is not randomness; it is **error**. The question is where an error is tolerable. |

So the risk is **not** that RouteForge would behave unpredictably. The model gives the same
answer for the same input, apart from tiny numeric jitter. The risk is that it gives a
**stable wrong answer** for some inputs, and that RouteForge's audit story ("every decision is
explainable from the trace") gets weaker when the explanation is "a 27B model said 0.73".

## 3. Role by role: good idea or not

The test for each role is what happens when the model is wrong for a request, and whether a
guarantee breaks.

### 3.1 Privacy gate, residency, eligibility: **no**
- **If it's wrong:** a false negative on "contains PII" sends sensitive data to a cloud model.
  That breaks rule 1 and rule 3, the product's core promise.
- **Allowed use:** it can only **tighten**, for example by adding `onprem_only` when it is
  confident the prompt is sensitive, alongside the existing Presidio/regex detector. That is
  the same "raise-only" pattern as `sensitivity_rules` (CLAUDE.md rule 2a).
- **Never allowed:** relaxing, or being the only detector.
- **Architecture:** it would have to run **after** the gate and **on-prem**, because it reads
  the raw prompt.

### 3.2 Step-9 verifier (block or escalate bad answers): **not as a hard gate**
- **If it's wrong:** false "bad" verdicts block or escalate good answers, which adds cost and
  latency to every request. False "good" verdicts miss leaks the regex verifier would catch.
- **Allowed use:** a **sampled advisory signal** recorded on the trace, never the blocker.

### 3.3 Live tier / routing classifier (step 6): **maybe, later**
- **If it's wrong:** a hard task goes to a cheap model, or an easy one to an expensive model.
  The effect is cost and quality, **not** privacy: the router only reorders the eligible set,
  so a wrong classification cannot reach an ineligible model.
- **Why it's attractive:** today's tier choice is prompt length (`tokens/2000`), which is not
  semantic. A decision model could say "this is a hard reasoning task", and that is the gap
  noted in the tier-selection memo.
- **Why it's second:**
  - **Latency:** it adds a 27B forward pass on every request. The cards claim 70–500 ms, and
    on the Spark it would compete with Qwen and Nemotron for GPU memory (~49 GiB BF16).
  - **Determinism of routing:** you would need to show on your own traffic that it beats the
    length heuristic before paying for it.
- **Guard-rails if adopted:** it is a scoring input (like quality), never a filter. Its
  probability and model version go on the trace, and a low-confidence answer falls back to
  today's rule (§4).

### 3.4 Offline or sampled label source for P2b-3 calibration: **yes, first**
- **If it's wrong:**
  - **Noise.** One noisy label barely moves a Beta posterior, and the estimator already
    requires about 24 labels and a narrow interval before it recommends anything.
  - **Human in the loop.** Every recommendation still goes through the replay diff and an
    admin apply. A bad judge produces a bad **recommendation**, which a human sees before
    anything changes.
- **Why it's the best fit:**
  - It directly fixes P2b-3's weakest point: labels are sparse.
  - Its errors are averaged away, not acted on individually.
  - It changes nothing at request time.

## 4. If adopted: the guard-rails that make probabilistic decisions acceptable

These make a probabilistic decision **auditable and reproducible** even though the model is
not a rule.

1. **Record, don't recompute.** Store the probabilities, the question, the model id and hash,
   and the threshold on the trace or outcome row. The decision can then be reproduced from the
   record even if re-running the model gives 0.501 instead of 0.499. This is the same principle
   as the P2a scoring record.
2. **A dead band around thresholds.** Treat, say, 0.45–0.55 as "unsure" and fall back to the
   deterministic rule. This removes almost all run-to-run decision flips from numeric jitter.
3. **Pin the model and serve it deterministically.** Use a fixed weight hash, a fixed
   `max_model_len`, and batch size 1 (or vLLM's deterministic mode) for decisions that are
   recorded. A model upgrade is an onboarding event with its own version, never a silent
   swap.
4. **Asymmetric authority.** It may only make a decision **safer** (more on-prem, more
   verification). Anything that relaxes a protection stays rule-based.
5. **Content stays in the boundary.** The model reads raw prompts and completions, so it must
   be an on-prem endpoint. Only numbers leave it. That fits the content-free outcome tables.
6. **Measure before trusting.** Run an agreement study on your own workloads before any use:
   judge vs human labels, and judge vs the deterministic graders in FeasibleRouteBench. The
   published accuracy numbers are the author's own benchmark, not your traffic.

## 5. Specific concerns with AutoJev-27B as the candidate

- **Self-preference bias.** AutoJev-27B is fine-tuned from **Qwen3.8-27B**, which is also one
  of the models RouteForge serves on the Spark. A judge built on the same base model tends to
  rate that model's answers higher. For calibration, that would push traffic toward Qwen for
  the wrong reason. **This is the biggest practical risk.** Mitigations:
  - check agreement specifically on Qwen-served vs other-served answers;
  - or use a judge from a different model family.
- **Evidence quality.**
  - The accuracy and calibration numbers come from the author's own held-out set.
  - Two sources give different figures: 84.6% on the model card, 91.9% on the SFT release.
  - The training corpus is not published.
  - The model was produced "through autonomous agents".
  - None of this disqualifies it, but none of it is independent evidence either (the
    LangWatch "best Jev" benchmark issue is the kind of comparison to wait for).
- **Cost to run.** About 49 GiB in BF16. On the Spark it only fits by replacing a served model,
  or in a quantised form whose calibration would need re-checking.
- **Licence.** Weights are Apache-2.0 and code is MIT, which is fine for on-prem use. The card
  notes some weights need Hugging Face authentication.

## 6. What would make it a clear "go"

1. **A judge from a different family** than the models being judged, or proof on your data that
   self-preference is small.
2. **Independent evaluation,** for example the LangWatch comparison, showing calibration holds
   outside the author's test set.
3. **A version that fits the Spark next to the served models,** or a separate GPU for it.
4. **Your own agreement study.** About 200 real requests per workload, labelled by a human, with
   agreement ≥ 85% and no bias toward one served model.

Once those hold, the build is small:
- run a sampled, in-boundary judge on a fraction of `ok` requests;
- write its verdict as `outcome_feedback` with `source="judge"`;
- let P2b-3 weight or filter by source.

No routing change is needed until the labeler has earned trust.

## 7. Sources
- [denis-pplx/autojev-27b (model card)](https://huggingface.co/denis-pplx/autojev-27b)
- [denis-pplx/autojev-sft-27b](https://huggingface.co/denis-pplx/autojev-sft-27b) · [GitHub autojev-sft](https://github.com/denis-pplx/autojev-sft)
- [LangWatch: benchmark of Jev-like models (issue #896)](https://github.com/langwatch/tasks/issues/896)
- [AgentJev-0.6B (a small System One model)](https://github.com/TheTechOddBug/agent-jev)
- The owner's original notes (TypeSafe blog and a router walkthrough): Appendix A below

---

## Appendix A. Owner's original notes (captured from `jev-model.txt`)

These are the notes that prompted this memo, kept verbatim apart from formatting. They describe a third-party demo router, not RouteForge; its figures are the demo's claims and were not measured here.

Sources:
- <https://typesafe.ai/blog/introducing-system-one-models-and-jev>
- <https://gist.github.com/antoniolg/62f82f2a5d191fe074e3f5065501993d>

Building Intelligent Model Routers with Jev and OpenJev

Executive Summary

This briefing document examines the implementation and benefits of Jev—a "system one" model from Typesafe AI—and its open-source counterparts (OpenJev) in the context of building intelligent model routers. Unlike traditional Large Language Models (LLMs) that generate text, Jev operates as a high-speed decision engine that evaluates input "states" against typed questions to return probabilities and confidence scores.

By integrating Jev into a local endpoint architecture, developers can automate the routing of user prompts to the most appropriate model based on task category, difficulty, and privacy requirements. This approach significantly reduces operational costs (4 cents per million input tokens), minimizes latency (70–500ms), and ensures data security by gating Private Identifiable Information (PII) locally. The system allows for a hybrid environment where simple tasks stay local, while complex or sensitive tasks are routed to specialized frontier models or kept entirely offline.

Overview of Jev Technology

Jev is defined as a "system one" model that does not generate text. Instead, it provides a structured framework for making conditional logic decisions based on natural language.

Core Mechanics

* State and Questions: The user provides a "state" (the input to be judged) and a set of "typed questions."
* Outputs: Jev returns typed answers accompanied by probabilities and confidence values.
* Programming Integration: These outputs are used to drive standard if/else statements in code, eliminating the need to prompt models for JSON compliance or complex parsing.

Types of Inquiries

Jev and OpenJev clones support three primary question formats:

Question Type	Function	Application Example
Choice	Categorization into discrete classes.	Determining if a prompt is "chitchat," "code," or "image generation."
Score	Positioning on a scale (up to 10 ordered levels).	Assessing the "difficulty" of a task to choose a model tier.
Null	Boolean (Yes/No) evaluation with a probability.	Detecting PII or determining if web search is required.

Architectural Implementation: The Model Router

The source context outlines a functional architecture for a model router using a Fast API server (Python) and a Next.js UI. This system acts as an intermediary that intercepts prompts and directs them to specific "lanes."

Decision Lanes and Routing Logic

The router uses parallel evaluation—asking multiple questions in a single request—to determine the prompt's path:

1. Privacy Gate (Null Question): Detects PII, financial data, or secrets. If the probability of sensitive data is high (e.g., > 0.5), the prompt is locked to local models regardless of other factors.
2. Category Selection (Choice Question): Identifies the task type.
  * Chitchat/Simple Questions: Directed to small, local models.
  * Code/Analysis: Directed to high-performance cloud models.
  * Image Requests: Directed to local Vision-Language Models (VLMs).
3. Capability Assessment (Score Question): Measures task difficulty from "trivial" to "research-grade." This determines if a local 2B model is sufficient or if a frontier model like Claude Opus is required.

Integrated Models

The system demonstrated utilizes a mix of local and cloud-based assets:

* Local Models: Mini CPM 52B (for general tasks and prompt rewriting), Quen image 2.1 (for image generation), and SEMIF (for local OpenJev logic).
* Cloud Models: Deepseek V4.1 Flash and Claude Opus (accessed via Open Router).

Strategic Benefits

1. Cost Efficiency

Jev offers a dramatic reduction in overhead compared to using LLMs for classification:

* Input Costs: Approximately 4 cents per million input tokens.
* Output Costs: Zero charge, as the model does not generate text tokens.
* Local Savings: In test scenarios, routing 75% of requests to local models resulted in significant cost avoidance while maintaining performance.

2. High Performance and Low Latency

* Response Times: Jev typically returns decisions within the 70ms to 500ms range.
* Parallelism: Multiple questions (Choice, Score, Null) are evaluated simultaneously in one request, ensuring that complex routing logic does not add incremental latency.
* Local OpenJev: Using local versions like SEMIF can reduce decision latency to as low as 88ms while keeping the entire decision-making process off-cloud.

3. Data Privacy and Security

By using Jev (or OpenJev locally) as a gatekeeper, the system can ensure that sensitive data never reaches cloud providers. The router identifies PII before any external API calls are made, allowing the system to "fail safe" by defaulting to local processing for any suspicious or sensitive input.

4. Reliability

Because Jev provides a confidence score, the router can identify when the model is unsure of a classification. In cases of low confidence, the system can be programmed to fall back to a safe default (such as a high-capability general model) rather than making an incorrect routing guess.

Key Performance Observations

The following data points highlight the practical efficacy of the Jev-based router:

* Decision Speed: SEMIF (local) achieved 88ms for a privacy and lane-routing decision.
* Accuracy: Successfully distinguished between simple "chitchat" (routed to local 2B models) and "code" (routed to Deepseek Flash) with high probability scores (e.g., 0.96 for PII detection).
* Scalability: Demonstrated the ability to handle full chat histories by using caching and maintaining model consistency for long-form conversations.
* Economic Impact: Entire demonstration sequences cost less than one cent in API fees, illustrating the feasibility of high-volume agentic use cases.
