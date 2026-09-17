"""A deployment upgrading past the 2026-09-17 agent removal still has
`agent:`, `agent_budgets:`, and `stage_signals:` in its mounted YAML. Unknown
keys must be ignored, never a boot failure."""
import shutil
from pathlib import Path

import yaml

from app.config.lint import enforce
from app.config.loader import load_settings


def test_boot_ignores_stale_agent_keys(tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(Path("config"), cfg)

    serving = yaml.safe_load((cfg / "serving.yaml").read_text())
    serving["agent"] = {"runtime": "nat"}
    (cfg / "serving.yaml").write_text(yaml.safe_dump(serving))

    policy = yaml.safe_load((cfg / "policy.yaml").read_text())
    policy["agent_budgets"] = {"max_steps": 5, "max_spend_usd": 0.05,
                               "per_call_timeout_s": 10}
    (cfg / "policy.yaml").write_text(yaml.safe_dump(policy))

    routing = yaml.safe_load((cfg / "routing.yaml").read_text())
    routing["stage_signals"] = {"enabled": False, "role_tiers": {"plan": "frontier"}}
    (cfg / "routing.yaml").write_text(yaml.safe_dump(routing))

    settings = load_settings(config_dir=str(cfg))
    enforce(settings)  # must not raise

    assert not hasattr(settings.routing, "stage_signals")
    assert not hasattr(settings.policy, "agent_budgets")
