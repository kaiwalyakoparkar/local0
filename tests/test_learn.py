"""POST /learn — gateway callback stores cloud Q&A when query matches LEARN_TAGS."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, main, rag  # noqa: E402

client = TestClient(main.app)


def test_tag_match_substring(monkeypatch):
    monkeypatch.setattr(config, "get_learn_tags", lambda: ["refund", "shipping"])
    assert config.tag_match("What is the refund policy?")
    assert not config.tag_match("What is the weather?")


def test_learn_skips_when_no_tag_match(monkeypatch):
    monkeypatch.setattr(config, "get_learn_tags", lambda: ["refund"])
    called = {"upsert": False}

    def boom(*a, **k):
        called["upsert"] = True
        raise AssertionError("should not upsert")

    monkeypatch.setattr(rag, "upsert_learned", boom)
    r = client.post("/learn", json={"query": "weather today?", "answer": "sunny"})
    assert r.status_code == 200
    assert r.json() == {"stored": False, "reason": "no tag match"}
    assert called["upsert"] is False


def test_learn_upserts_on_tag_match(monkeypatch):
    monkeypatch.setattr(config, "get_learn_tags", lambda: ["refund"])
    captured = {}

    def fake_upsert(query, answer):
        captured["query"] = query
        captured["answer"] = answer

    monkeypatch.setattr(rag, "upsert_learned", fake_upsert)
    r = client.post(
        "/learn",
        json={"query": "What is the refund policy?", "answer": "30 days"},
    )
    assert r.status_code == 200
    assert r.json() == {"stored": True}
    assert captured == {
        "query": "What is the refund policy?",
        "answer": "30 days",
    }


def test_learn_bad_body():
    r = client.post("/learn", json={"query": "x"})
    assert r.status_code == 400
