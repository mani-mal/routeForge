import os
import subprocess


def test_workflow_validates():
    # nat validate must accept the workflow (agents + tools discoverable)
    env = dict(os.environ)
    r = subprocess.run(
        ["uv", "run", "--extra", "agent", "--extra", "observability",
         "nat", "validate", "--config_file", "config/nat_workflow.yaml"],
        capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, f"nat validate failed:\n{r.stdout}\n{r.stderr}"
