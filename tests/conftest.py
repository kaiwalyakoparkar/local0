"""Keep tests off the real .env.

config._set_env_key writes ROOT/.env, so any test that sets a threshold or tags
(directly or via /config) used to clobber the repo's .env — poisoning a fresh
`make up` with stale values. Redirect ENV_PATH to a per-test tmp file instead.
"""
import pytest

from app import config


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
