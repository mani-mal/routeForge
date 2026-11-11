"""B89 acceptance: the Helm charts must render a config the api can boot on.

For each chart (and serving-plane with its GreenLake overlay) this runs
``helm template`` at default values, pulls the rendered ``serving.yaml``
ConfigMap out, drops it next to the repo's shipped routing/policy/pricing
configs and runs the same ``load_settings()`` + ``enforce()`` the api runs at
boot. A chart that renders a provider without ``litellm_prefix`` (the original
B89 failure, ``PROVIDER_NO_LITELLM_MAP``) fails here instead of at install.

It also pins the other B89 contracts: the admin password is generated (never
``changeme``), and the serving-plane UI is wired to the api Service rather
than to Docker's DNS.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.config.lint import enforce, lint_settings
from app.config.loader import load_settings

REPO = Path(__file__).resolve().parent.parent
SERVING_PLANE = REPO / "deploy" / "helm" / "serving-plane"
HPE = REPO / "deploy" / "hpe" / "routeforge"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is not installed")

# (id, chart dir, extra helm args, admin-password values key)
CASES = [
    ("serving-plane", SERVING_PLANE, [], "admin.password"),
    ("serving-plane-greenlake", SERVING_PLANE,
     ["-f", str(SERVING_PLANE / "values-greenlake.yaml")], "admin.password"),
    ("hpe", HPE, [], "credentials.adminPassword"),
]


def _template(chart: Path, *args: str) -> list[dict]:
    proc = subprocess.run(
        ["helm", "template", "rel", str(chart), "--namespace", "ns", *args],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


@pytest.mark.parametrize("name,chart,args,_key", CASES, ids=[c[0] for c in CASES])
def test_rendered_serving_yaml_passes_boot_lint(
        name, chart, args, _key, tmp_path, monkeypatch):
    docs = _template(chart, *args)
    cms = [d for d in _kind(docs, "ConfigMap") if "serving.yaml" in d.get("data", {})]
    assert len(cms) == 1, "expected exactly one ConfigMap carrying serving.yaml"

    for f in ("routing.yaml", "policy.yaml", "pricing.yaml"):
        shutil.copy(REPO / "config" / f, tmp_path / f)
    (tmp_path / "serving.yaml").write_text(cms[0]["data"]["serving.yaml"])

    # Settings must come from the rendered file, not from the test runner's env.
    import os
    for var in list(os.environ):
        if var.startswith(("SP_", "LANGFUSE_")):
            monkeypatch.delenv(var)

    settings = load_settings(str(tmp_path))
    # Named violations first (e.g. PROVIDER_NO_LITELLM_MAP), then the full
    # boot gate (enforce() exits non-zero on any violation).
    assert lint_settings(settings) == []
    enforce(settings)


@pytest.mark.parametrize("name,chart,args,key", CASES, ids=[c[0] for c in CASES])
def test_changeme_admin_password_refuses_to_render(name, chart, args, key):
    proc = subprocess.run(
        ["helm", "template", "rel", str(chart), *args, "--set", f"{key}=changeme"],
        capture_output=True, text=True)
    assert proc.returncode != 0
    assert "changeme" in proc.stderr


@pytest.mark.parametrize("name,chart,args,_key", CASES, ids=[c[0] for c in CASES])
def test_default_render_generates_admin_password(name, chart, args, _key):
    docs = _template(chart, *args)
    pw = [
        d["stringData"]["ADMIN_PASSWORD"]
        for d in _kind(docs, "Secret")
        if "ADMIN_PASSWORD" in d.get("stringData", {})
    ]
    assert len(pw) == 1
    assert len(pw[0]) >= 20 and pw[0] != "changeme"


def test_serving_plane_ui_nginx_points_at_api_service():
    docs = _template(SERVING_PLANE)  # _template renders with --namespace ns
    ui = next(d for d in _kind(docs, "Deployment")
              if d["metadata"]["name"].endswith("-ui"))
    pod = ui["spec"]["template"]["spec"]
    cm_names = [v["configMap"]["name"] for v in pod["volumes"] if "configMap" in v]
    assert len(cm_names) == 1
    mounts = pod["containers"][0]["volumeMounts"]
    assert any(m["mountPath"].startswith("/etc/nginx/conf.d/") for m in mounts)

    cm = next(d for d in _kind(docs, "ConfigMap") if d["metadata"]["name"] == cm_names[0])
    conf = next(iter(cm["data"].values()))
    sub_paths = [m["subPath"] for m in mounts if m["mountPath"].startswith("/etc/nginx/conf.d/")]
    assert sub_paths and all(sp in cm["data"] for sp in sub_paths)
    svc = next(d for d in _kind(docs, "Service") if d["metadata"]["name"].endswith("-api"))
    api_svc = svc["metadata"]["name"]
    port = svc["spec"]["ports"][0]["port"]
    assert f"{api_svc}.ns.svc.cluster.local:{port}" in conf
    assert "127.0.0.11" not in conf
    for loc in ("/api/stream", "/api/", "/v1/", "/admin/"):
        assert f"location {loc}" in conf


# --reuse-values simulation. `helm upgrade --reuse-values` replaces the chart's
# new defaults with the old release's stored values, so keys added in this
# version (admin:, providers.*.litellmPrefix) are absent. `helm template` has
# no such mode, so we null those keys out with --set (Helm deletes a key set to
# null), which drives exactly the nil paths the templates must tolerate.
REUSE_CASES = [
    ("serving-plane", SERVING_PLANE,
     ["--set", "admin=null"]),
    ("hpe", HPE,
     ["--set", "credentials.adminPassword=null", "--set", "credentials.adminEmail=null"]),
]


@pytest.mark.parametrize("name,chart,extra", REUSE_CASES, ids=[c[0] for c in REUSE_CASES])
def test_renders_when_new_keys_are_missing_as_after_reuse_values(
        name, chart, extra, tmp_path, monkeypatch):
    args = [*extra]
    for p in ("onprem", "azure", "bedrock"):
        args += ["--set", f"servingPlane.providers.{p}.litellmPrefix=null"]
    docs = _template(chart, *args)
    cms = [d for d in _kind(docs, "ConfigMap") if "serving.yaml" in d.get("data", {})]
    for f in ("routing.yaml", "policy.yaml", "pricing.yaml"):
        shutil.copy(REPO / "config" / f, tmp_path / f)
    (tmp_path / "serving.yaml").write_text(cms[0]["data"]["serving.yaml"])
    import os
    for var in list(os.environ):
        if var.startswith(("SP_", "LANGFUSE_")):
            monkeypatch.delenv(var)
    settings = load_settings(str(tmp_path))
    assert lint_settings(settings) == []
    assert settings.serving.providers["onprem"].litellm_prefix == "openai"
    assert any(len(d["stringData"]["ADMIN_PASSWORD"]) >= 20
               for d in _kind(docs, "Secret") if "ADMIN_PASSWORD" in d.get("stringData", {}))
