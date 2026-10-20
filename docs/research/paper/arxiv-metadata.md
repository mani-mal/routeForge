# arXiv metadata — paste these into the submission wizard

All fields are plain text (arXiv does not render LaTeX).

## Title

Eligibility-Constrained LLM Routing: When the Candidate Pool Is a Per-Request Variable

(Optional alternative suggested by the pre-arXiv review: "RouteForge: Policy-Constrained LLM
Routing and Replay in Mixed On-Premises and Cloud Deployments". If you switch, change `\title`
in main.tex too so the PDF and metadata agree.)

## Authors

Mani Malarvannan

(The PDF byline reads "Mani Malarvannan", mani.malarvannan@cdw.com. No affiliation line is
set; add one in `\author{...}` and here if the paper should carry it.)

## Abstract

LLM gateways in mixed on-premises and cloud deployments must restrict which endpoints may receive each request before they optimise model selection. Where compute may run, whether a provider keeps prompts, its region, what a tenant has allowed and an estimate of what a request will cost together decide which endpoints may see a request at all. We present RouteForge, a self-hosted, OpenAI-compatible gateway that derives this eligible set per request and rejects the request when it is empty. RouteForge combines a policy compiler whose placement and protection components are tested for independence with a property-based harness, an evaluation harness whose oracle never credits an endpoint a request was not allowed to use, and a replay analyser for proposed policy changes. The compiler checks found two fail-open defects. Across four live runs on synthetic traffic, 3,611 requests in all, none of the 987 requests that the compiled policy confined to on-premises compute was attempted on a cloud endpoint. Replay matched the recorded first-decision admission (routed or rejected) of every request; the identity of the serving endpoint diverged during outages, because replay omits operational failure state. A single prospective comparison predicted 180 cloud-bound requests and observed 178. The evaluation also exposed weaknesses: on a benchmark at which every model passed, the length-based router paid about 6.7 times the cheapest eligible stated price. Finally, a compaction example shows a restriction raised by a tool result being lost across calls, and motivates a prototype that preserves detector-derived placement restrictions within a configured identity and lifetime. These results support policy-aware testing and operational analysis, not a claim of better routing quality, of general privacy, or of calibrated prediction under arbitrary traffic.

## Categories

- Primary: cs.SE (Software Engineering) — the contribution is a system plus its compiler
  testing, evaluation harness and replay tooling.
- Cross-lists: cs.DC (Distributed, Parallel, and Cluster Computing), cs.CR (Cryptography and
  Security)

## Comments

23 pages, 1 figure, 12 tables. Code and data: https://github.com/mani-mal/routeForge (tag arxiv-v1)

## Optional fields — leave blank

- Report number: blank
- Journal reference: blank
- DOI: blank
- MSC class: blank
- ACM class (optional): C.2.4; D.2.5; K.6.5

## License

arXiv non-exclusive license (default) unless you want a Creative Commons license. The paper
license is irrevocable for the posted version and is separate from the code license in the
repository.
