"""Plain-filter eligibility baseline, written from the documented semantics
(docs/reference/per-app-config.md "Model eligibility is two-sided" and "Endpoint
data handling"; docs/guides/onboarding-models.md (e2)). It does not import
app.policy.feasible or app.policy.core. Input is plain dicts; output is a set of
endpoint names. No reason, no code, no core.
"""


def declared(v):
    """An endpoint declaration: lowercased and stripped; undeclared reads 'unknown'."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str) and v.strip():
        return v.strip().lower()
    return "unknown"


def eligible(req, endpoints):
    providers = set(req["allowed_providers"])
    if req["onprem_only"]:
        providers &= {"onprem"}
    regions = None if req["allowed_regions"] is None else {r.lower() for r in req["allowed_regions"]}
    out = set()
    for e in endpoints:
        if e["provider"] not in providers:
            continue
        if regions is not None and declared(e["region"]) not in regions:
            continue
        if req["require_no_retention"] and declared(e["prompt_retention"]) != "none":
            continue
        if req["sends_raw_sensitive"] and declared(e["accepts_raw_sensitive"]) == "false":
            continue
        if e["name"] in req["exclude"]:
            continue
        if req["allowed_tiers"] is not None and e["tier"] not in req["allowed_tiers"]:
            continue
        if req["allowed_models"] is not None and e["name"] not in req["allowed_models"]:
            continue
        if req["requested_model"] is not None and e["name"] != req["requested_model"]:
            continue
        if e["name"] in req["app_denied"] or e["disabled"]:
            continue
        if e["name"] in req["open_circuits"]:
            continue
        out.add(e["name"])
    return out
