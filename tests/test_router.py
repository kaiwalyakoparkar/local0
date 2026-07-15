"""Router gate — the 4 Phase-3 cases: 200 / 424 / 400 malformed / 400 stream.

Mocks retrieval + Ollama so no live services are needed. Run: pytest tests/
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import main, rag, ollama, config  # noqa: E402

client = TestClient(main.app)
MSG = {"messages": [{"role": "user", "content": "what is X?"}]}


def _patch(monkeypatch, top_score):
    monkeypatch.setattr(rag, "retrieve", lambda q, k=4: ([{"text": "ctx", "source": "d"}], top_score))
    monkeypatch.setattr(ollama, "chat", lambda m: "local answer")
    monkeypatch.setattr(config, "get_threshold", lambda: 0.55)


def test_200_local(monkeypatch):
    _patch(monkeypatch, top_score=0.9)
    r = client.post("/v1/chat/completions", json=MSG)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "local answer"


def test_424_escalate(monkeypatch):
    _patch(monkeypatch, top_score=0.2)
    r = client.post("/v1/chat/completions", json=MSG)
    assert r.status_code == 424
    assert r.json() == {"detail": "no local context, escalate"}


def test_400_malformed():
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 400


def test_400_no_user_message():
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "assistant", "content": "hi"}]})
    assert r.status_code == 400


def test_400_stream():
    r = client.post("/v1/chat/completions", json={**MSG, "stream": True})
    assert r.status_code == 400


def test_config_localhost_ok(monkeypatch):
    # TestClient reports client host as "testclient" — treat it as loopback for the test.
    monkeypatch.setattr(main, "_LOOPBACK", main._LOOPBACK | {"testclient"})
    r = client.post("/config", json={"threshold": 0.42})
    assert r.status_code == 200
    assert r.json()["threshold"] == 0.42
    config.set_threshold(0.55)  # restore
