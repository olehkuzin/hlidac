"""WP32: the release's container group keeps secrets secret, keeps the app port private and forces
HTTPS settings. Invented values only."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("aci_spec", ROOT / "deploy" / "aci_spec.py")
aci_spec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aci_spec)

ENV = {
    "ACI_NAME": "aci-test",
    "LOCATION": "swedencentral",
    "DNS_LABEL": "hlidac-test",
    "IMAGE": "acr.example/hlidac-backend:release-1",
    "PROXY_IMAGE": "acr.example/caddy:2.8.4",
    "ACR_LOGIN_SERVER": "acr.example",
    "ACR_USERNAME": "acr",
    "ACR_PASSWORD": "acr-secret",
    "SECRET_KEY": "s" * 40,
    "IRIS_PASSWORD": "iris-secret",
    "IRIS_HOST": "iris.example",
    "SMTP_HOST": "$(SMTP_HOST)",  # missing from the variable group
}


def containers(group: dict) -> dict:
    return {c["name"]: c["properties"] for c in group["properties"]["containers"]}


def test_secrets_are_secure_values_and_settings_are_forced():
    app = containers(aci_spec.spec(aci_spec.pipeline_env(ENV)))["app"]
    env = {e["name"]: e for e in app["environmentVariables"]}
    assert env["SECRET_KEY"] == {"name": "SECRET_KEY", "secureValue": "s" * 40}
    assert env["IRIS_PASSWORD"] == {"name": "IRIS_PASSWORD", "secureValue": "iris-secret"}
    assert all("value" not in env[n] for n in aci_spec.SECRET if n in env)
    assert env["STORE_BACKEND"]["value"] == "iris"
    assert env["SESSION_HTTPS_ONLY"]["value"] == "true"
    assert env["APP_BASE_URL"]["value"] == "https://hlidac-test.swedencentral.azurecontainer.io"
    assert "SMTP_HOST" not in env  # an unset pipeline variable is not a value


def test_skip_login_is_always_off_behind_the_proxy():
    group = aci_spec.spec(aci_spec.pipeline_env({**ENV, "DEMO_SKIP_LOGIN": "true"}))
    env = containers(group)["app"]["environmentVariables"]
    assert [e["value"] for e in env if e["name"] == "DEMO_SKIP_LOGIN"] == ["false"]


def test_every_app_setting_is_passed_forced_or_deliberately_left_out():
    from hlidac.config import Settings

    forced = {"STORE_BACKEND", "SESSION_HTTPS_ONLY", "OUTBOX_RUNNER", "APP_BASE_URL", "DEMO_SKIP_LOGIN"}
    left_out = {"WHISPER_MODEL", "LOCAL_EMBEDDING_MODEL"}  # mlx needs a Mac; embeddings unused
    known = set(aci_spec.PLAIN) | set(aci_spec.SECRET) | forced | left_out
    assert {name.upper() for name in Settings.model_fields} <= known


def test_only_the_https_proxy_is_public():
    group = aci_spec.spec(aci_spec.pipeline_env(ENV))
    assert [p["port"] for p in group["properties"]["ipAddress"]["ports"]] == [80, 443]
    proxy = containers(group)["https"]
    assert proxy["command"][-1] == "localhost:8000"


@pytest.mark.parametrize("name", ["SECRET_KEY", "IRIS_PASSWORD", "IMAGE"])
def test_a_missing_required_variable_stops_the_release(name):
    with pytest.raises(SystemExit):
        aci_spec.spec(aci_spec.pipeline_env({**ENV, name: f"$({name})"}))


def test_the_pipeline_hands_every_setting_to_the_deploy_script():
    pipeline = yaml.safe_load((ROOT / "azure-pipelines.yml").read_text())
    [deploy] = [s for s in pipeline["stages"] if s["stage"] == "Deploy"]
    steps = deploy["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"]
    [cli] = [s for s in steps if s.get("task") == "AzureCLI@2"]
    assert set(aci_spec.PLAIN) | set(aci_spec.SECRET) <= set(cli["env"])


def test_the_pipeline_runs_only_on_release_tags():
    pipeline = yaml.safe_load((ROOT / "azure-pipelines.yml").read_text())
    assert pipeline["trigger"] == {"branches": {"exclude": ["*"]}, "tags": {"include": ["release-*"]}}
    assert pipeline["pr"] == "none"
