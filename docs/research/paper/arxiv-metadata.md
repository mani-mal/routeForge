# arXiv metadata — paste these into the submission wizard

All fields are plain text (arXiv does not render LaTeX).

## Title

RouteForge: Testing and Replaying Policy-Constrained LLM Routing

(Earlier draft title: "Eligibility-Constrained LLM Routing: When the Candidate Pool Is a Per-Request Variable". The title above matches `\title` in main.tex.)

## Authors

Mani Malarvannan

(The PDF byline reads "Mani Malarvannan", CDW, mani.malarvannan@cdw.com.)

## Abstract

An LLM gateway that serves several applications over on-premises and cloud endpoints must decide which endpoints may receive a request before it chooses among them. We present RouteForge, a self-hosted, OpenAI-compatible gateway that computes this eligible set for every request from operator-declared application policy, endpoint declarations and detected sensitive content, and rejects the request when the set is empty. We study three things around it. First, enforcement: a property-based harness for the policy compiler found two fail-open defects, and in two live runs with cloud endpoints none of the 945 outbound attempts made for the 987 requests confined to on-premises compute went to a cloud endpoint. Second, evaluation: a benchmark whose oracle credits only eligible endpoints showed that the length-based router paid about 6.7 times the cheapest eligible stated price when every model answered correctly. Third, replay: re-running recorded, content-free routing decisions under a proposed policy reproduced the admission decision of all 3,611 requests in four live runs, but not which endpoint served during outages, and one prospective prediction of the cloud-bound count (180) was close to the later observation (178). All traffic is synthetic. We also show a restriction lost when an agent compacts its context, and a prototype that carries detector-derived placement restrictions across related calls.

## Categories

- Primary: cs.SE (Software Engineering) — the contribution is a system plus its compiler
  testing, evaluation harness and replay tooling.
- Cross-lists: cs.DC (Distributed, Parallel, and Cluster Computing), cs.CR (Cryptography and
  Security)

## Comments

28 pages, 1 figure, 16 tables. Code and data: https://github.com/mani-mal/routeForge (tag arxiv-v4)

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
