# Eligibility predicates of RouteForge, written as a generic policy engine would
# express them (Rego, OPA 0.70.0), from docs/reference/per-app-config.md
# ("Model eligibility is two-sided", "Endpoint data handling") and
# docs/guides/onboarding-models.md (e2). It does NOT import or read app/policy/*.
#
# Input (built by matched_baseline.py from the same gate output the shipped
# filter reads):
#   input.sets[<name>].endpoints[]  {name, provider, tier, region, prompt_retention,
#                                    accepts_raw_sensitive, disabled}
#   input.sets[<name>].requests[]   {id, onprem_only, allowed_providers[],
#                                    allowed_regions[]|null, require_no_retention,
#                                    sends_raw_sensitive, allowed_tiers[]|null,
#                                    allowed_models[]|null, requested_model|null,
#                                    exclude[], app_denied[], open_circuits[]}
# Output: data.eligibility.batch[<set>][<request id>] = sorted endpoint names.
package eligibility

import rego.v1

# --- endpoint declarations: lowercase+strip, anything undeclared reads "unknown"
declared(v) := "unknown" if {
	v == null
}

declared(v) := "true" if v == true

declared(v) := "false" if v == false

declared(v) := out if {
	is_string(v)
	t := lower(trim_space(v))
	t != ""
	out := t
}

declared(v) := "unknown" if {
	is_string(v)
	lower(trim_space(v)) == ""
}

# --- the predicates (each true = the endpoint passes)
placement_ok(req, e) if {
	not req.onprem_only
	e.provider in req.allowed_providers
}

placement_ok(req, e) if {
	req.onprem_only
	e.provider == "onprem"
	e.provider in req.allowed_providers
}

region_ok(req, _) if req.allowed_regions == null

region_ok(req, e) if {
	req.allowed_regions != null
	declared(e.region) in {lower(r) | some r in req.allowed_regions}
}

retention_ok(req, _) if not req.require_no_retention

retention_ok(req, e) if {
	req.require_no_retention
	declared(e.prompt_retention) == "none"
}

# only an explicit "false" refuses raw sensitive content; "unknown" is kept
raw_ok(req, _) if not req.sends_raw_sensitive

raw_ok(req, e) if {
	req.sends_raw_sensitive
	declared(e.accepts_raw_sensitive) != "false"
}

not_failed(req, e) if not e.name in req.exclude

tier_ok(req, _) if req.allowed_tiers == null

tier_ok(req, e) if e.tier in req.allowed_tiers

model_ok(req, _) if req.allowed_models == null

model_ok(req, e) if e.name in req.allowed_models

pin_ok(req, _) if req.requested_model == null

pin_ok(req, e) if e.name == req.requested_model

app_ok(req, e) if not e.name in req.app_denied

enabled_ok(_, e) if not e.disabled

circuit_ok(req, e) if not e.name in req.open_circuits

allowed(req, e) if {
	placement_ok(req, e)
	region_ok(req, e)
	retention_ok(req, e)
	raw_ok(req, e)
	not_failed(req, e)
	tier_ok(req, e)
	model_ok(req, e)
	pin_ok(req, e)
	app_ok(req, e)
	enabled_ok(req, e)
	circuit_ok(req, e)
}

eligible_for(req, endpoints) := {e.name | some e in endpoints; allowed(req, e)}

# one evaluation for every request of every input set
batch[set_name][req.id] := eligible_for(req, s.endpoints) if {
	some set_name, s in input.sets
	some req in s.requests
}

# single request, for `opa bench` (per-request evaluation cost)
single := eligible_for(input.request, input.endpoints)
