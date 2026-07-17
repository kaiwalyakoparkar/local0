"""Router gate — Phase-3 cases: 200 / 424 / 400 malformed / stream ignored.

Mocks retrieval + Ollama so no live services are needed. Run: pytest tests/
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

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
    # stream:false opts into the JSON body (default is now SSE for Hermes).
    _patch(monkeypatch, top_score=0.9)
    r = client.post("/v1/chat/completions", json={**MSG, "stream": False})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "local answer"


def test_default_no_stream_flag_returns_sse(monkeypatch):
    # Hermes sends no stream flag but force-parses SSE — default must be SSE.
    _patch(monkeypatch, top_score=0.9)
    r = client.post("/v1/chat/completions", json=MSG)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")


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


def test_stream_returns_sse(monkeypatch):
    # Hermes/OpenHands send stream:true and parse SSE — return event-stream, not JSON.
    _patch(monkeypatch, top_score=0.9)
    r = client.post("/v1/chat/completions", json={**MSG, "stream": True})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert "data: " in r.text
    assert "[DONE]" in r.text
    assert "local answer" in r.text


def test_config_localhost_ok(monkeypatch):
    # TestClient reports client host as "testclient" — treat it as loopback for the test.
    monkeypatch.setattr(main, "_LOOPBACK", main._LOOPBACK | {"testclient"})
    r = client.post("/config", json={"threshold": 0.42})
    assert r.status_code == 200
    assert r.json()["threshold"] == 0.42
    config.set_threshold(0.55)  # restore


def test_config_tags_save(monkeypatch):
    monkeypatch.setattr(main, "_LOOPBACK", main._LOOPBACK | {"testclient"})
    r = client.post("/config", json={"tags": "warranty, returns"})
    assert r.status_code == 200
    assert r.json()["tags"] == ["warranty", "returns"]
    config.set_learn_tags(["refund", "shipping"])  # restore


def test_config_public_denied(monkeypatch):
    monkeypatch.setattr(main, "_is_local", lambda req: False)
    r = client.post("/config", json={"threshold": 0.42})
    assert r.status_code == 403
    assert r.json()["detail"] == "local access only"


def test_deploy_rejects_relative_urls(monkeypatch):
    # An empty/relative base_url or router_url bakes a malformed callout URL into
    # the gateway definition and 500s every escalation — reject at the boundary.
    monkeypatch.setattr(main, "_LOOPBACK", main._LOOPBACK | {"testclient"})
    body = {"mapi_base": "http://x", "router_url": "http://router-service:8081",
            "fallback": {"base_url": "", "api_key": "k"}}
    r = client.post("/gateway/deploy", json=body)
    assert r.status_code == 400
    assert "base_url" in r.json()["detail"]

    body["fallback"]["base_url"] = "https://api.openai.com/v1"
    body["router_url"] = "router-service:8081"  # no scheme
    r = client.post("/gateway/deploy", json=body)
    assert r.status_code == 400
    assert "router_url" in r.json()["detail"]


def _req(host: str):
    req = MagicMock()
    req.client.host = host
    return req


def test_is_local_loopback():
    assert main._is_local(_req("127.0.0.1"))
    assert main._is_local(_req("::1"))


def test_is_local_docker_private():
    assert main._is_local(_req("172.17.0.1"))
    assert main._is_local(_req("192.168.65.1"))


def test_is_local_public_denied():
    assert not main._is_local(_req("8.8.8.8"))
    assert not main._is_local(_req("1.2.3.4"))
