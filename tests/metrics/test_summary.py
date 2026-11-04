from datetime import datetime, timedelta, timezone

from app.metrics.compute import FeedbackRow, OutcomeRow, UsageRow, summarize

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def usage(rid, cost=1.0, model="m", app="a", saved=(None, None, None, None)):
    return UsageRow(rid, app, T0, model, "onprem", "ok", cost, 100, None,
                    saved[0], saved[1], saved[2], saved[3])


def outcome(rid, status="ok", workload=None, traj=None, reason=None, app="a", chosen="m"):
    return OutcomeRow(rid, app, T0, status, chosen, workload, traj, 10.0, reason, [])


def label(rid, value, minutes=0):
    return FeedbackRow(rid, T0 + timedelta(minutes=minutes), "task_success", value)


def test_empty_window():
    s = summarize([], [], [])
    assert s["spend"] == {"value": 0.0, "n": 0}
    assert s["completed"]["value"] is None and s["completed"]["insufficient"] is True
    assert s["successful_reported"] is None
    assert all(s["savings"][k] == 0.0 for k in
               ("routing", "prompt_cache", "compression", "semantic_cache", "total"))


def test_completed_excludes_policy_rejections_and_cancelled():
    statuses = (["ok"] * 30 + ["backend_failed"] * 3 + ["verify_failed"] + ["no_eligible"] * 5
                + ["cancelled"] * 2)
    outs = [outcome(f"r{i}", st) for i, st in enumerate(statuses)]
    c = summarize([], outs, [])["completed"]
    assert c["n"] == 34 and c["value"] == 30 / 34
    assert c["failures"] == 4 and c["policy_rejections"] == 5 and c["excluded"] == 2


def test_usage_without_outcome_counts_in_spend_only():
    s = summarize([usage("r1", 2.0), usage("r2", 3.0)], [outcome("r1")], [])
    assert s["spend"] == {"value": 5.0, "n": 2}
    assert s["completed"]["n"] == 1
    s = summarize([usage("r1", 2.0), usage("r2", 3.0)], [outcome("r1", workload="x")], [],
                  workload="x")
    assert s["spend"] == {"value": 2.0, "n": 1}


def test_task_grouping_and_unit():
    outs = [outcome("a", traj="t1"), outcome("b", traj="t1"), outcome("c")]
    cp = summarize([], outs, [])["cost_per_task"]
    assert cp["n_tasks"] == 2 and cp["task_unit"] == "task"
    cp = summarize([], [outcome("a"), outcome("b")], [])["cost_per_task"]
    assert cp["task_unit"] == "request" and cp["n_tasks"] == 2


def test_latest_label_wins(monkeypatch):
    import app.metrics.compute as c
    monkeypatch.setattr(c, "MIN_N_TASKS", 1)  # gate tested separately below
    s = summarize([usage("a")], [outcome("a")], [label("a", 0, 0), label("a", 1, 5)])
    assert s["cost_per_task"]["n_labelled"] == 1
    assert s["cost_per_task"]["successful"] == 1.0


def _many(n_label, n_success, n_unlabelled):
    us, outs, fb = [], [], []
    for i in range(n_label):
        us.append(usage(f"l{i}")); outs.append(outcome(f"l{i}"))
        fb.append(label(f"l{i}", 1 if i < n_success else 0))
    for i in range(n_unlabelled):
        us.append(usage(f"u{i}")); outs.append(outcome(f"u{i}"))
    return us, outs, fb


def test_cost_per_successful_task_uses_labelled_tasks():
    s = summarize(*_many(25, 20, 100))
    assert s["cost_per_task"]["successful"] == 25 / 20
    assert s["cost_per_task"]["attempted"] == 125 / 125


def test_reported_success_insufficient_below_20_tasks():
    r = summarize(*_many(19, 10, 0))["successful_reported"]
    assert r["insufficient"] is True and r["value"] is None and r["n"] == 19
    assert summarize(*_many(0, 0, 5))["successful_reported"] is None
    r = summarize(*_many(20, 10, 0))["successful_reported"]
    assert r["insufficient"] is False and r["value"] == 0.5 and r["apps_reporting"] == 1


def test_savings_skip_pre_change_rows():
    rows = [usage("a"), usage("b", saved=(1.0, 2.0, 3.0, 4.0))]
    sv = summarize(rows, [], [])["savings"]
    assert sv["excluded_rows"] == 1
    # tuple order follows UsageRow: compression, routing, prompt_cache, semantic_cache
    assert (sv["compression"], sv["routing"], sv["prompt_cache"], sv["semantic_cache"],
            sv["total"]) == (1.0, 2.0, 3.0, 4.0, 10.0)


def test_route_reason_counts():
    outs = [outcome("a", reason="failover"), outcome("b", reason="failover"),
            outcome("c", reason="placement"), outcome("d")]
    assert summarize([], outs, [])["route_reasons"] == {"failover": 2, "placement": 1}


def test_summary_cost_per_successful_task_is_gated_i2():
    cp = summarize(*_many(19, 10, 0))["cost_per_task"]
    assert cp["successful"] is None and cp["successful_insufficient"] is True
    cp = summarize(*_many(20, 10, 0))["cost_per_task"]
    assert cp["successful"] == 2.0 and cp["successful_insufficient"] is False


def test_empty_flag_and_placement_filtered_nulls():
    assert summarize([], [], [])["empty"] is True
    s = summarize([], [outcome("r", "no_eligible", chosen=None)], [])
    assert s["empty"] is False and s["completed"]["policy_rejections"] == 1
    s = summarize([], [outcome("r", "no_eligible", chosen=None)], [], placement_filtered=True)
    assert s["completed"]["policy_rejections"] is None and s["completed"]["excluded"] is None
