"""Settings: a .env copied from .env.example, with its empty lines, loads (empty = not set)."""

from pathlib import Path

from hlidac.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_as_copied_loads(monkeypatch, tmp_path):
    for name in ("IRIS_PORT", "AZURE_OPENAI_REASONING_EFFORT", "STORE_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    settings = Settings(_env_file=env)
    assert settings.iris_port is None
    assert settings.azure_openai_reasoning_effort is None
    assert settings.store_backend == "memory"
    assert settings.demo_skip_login is False
