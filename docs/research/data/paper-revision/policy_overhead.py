"""In-process timing of the policy-side steps (no network, no DB)."""
import statistics, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.policy.compile import compile_policy
from app.policy.core import REQUIREMENT_ORDER, minimum_hitting_set
from app.policy.feasible import feasible_set

S = load_settings(ROOT / "config")
print("pool size", len(S.routing.model_pool), "requirement ids", len(REQUIREMENT_ORDER))


def t(fn, n):
    for _ in range(min(200, n)):
        fn()
    xs = []
    for _ in range(n):
        a = time.perf_counter_ns(); fn(); xs.append(time.perf_counter_ns() - a)
    xs.sort()
    return statistics.median(xs) / 1000, xs[int(0.99 * len(xs))] / 1000


pol = AppPolicyView(allowed_regions=frozenset({"us"}), require_no_retention=True)
print("compile_policy us: median/p99", t(lambda: compile_policy(pol, S), 20000))
print("feasible_set us:   median/p99", t(lambda: feasible_set(S, None, pol, tenant_id="a"), 20000))
# Worst case for the exact core search: 11 candidates each failing one distinct id,
# so no subset smaller than all 11 covers them and every subset is enumerated.
worst = [frozenset({i}) for i in REQUIREMENT_ORDER]
print("core worst (k=%d) us:" % len(worst), t(lambda: minimum_hitting_set(worst), 2000))
typical = [frozenset({REQUIREMENT_ORDER[0], REQUIREMENT_ORDER[3]}),
           frozenset({REQUIREMENT_ORDER[1], REQUIREMENT_ORDER[2]})]
print("core typical us:", t(lambda: minimum_hitting_set(typical), 20000))
