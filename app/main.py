"""FastAPI router — OpenAI-compatible /v1/chat/completions with the 424 gate.

Per request:
  0. stream:true                    -> 400 (v1 unsupported)
  1. extract last user message      -> 400 if none
  2. retrieve(query) -> top_score
  3. top_score < THRESHOLD          -> 424 {"detail": "no local context, escalate"}
  4. else prompt Qwen with context  -> 200 OpenAI-compatible

The 424 is the escalation signal. A response-based routing policy on the gateway
(Phase 4) rewrites it into a reroute to the cloud provider — built-in failover
ignores HTTP status (Phase-0 smoke), so the policy is mandatory.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, ollama, rag, stats

app = FastAPI(title="local0", version="1.0.0")
_DASHBOARD = Path(__file__).parent / "dashboard.html"

ESCALATE_BODY = {"detail": "no local context, escalate"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _last_user_message(messages: list[dict]) -> str | None:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return None


def _build_prompt(chunks: list[dict], query: str) -> list[dict]:
    context = "\n\n".join(f"[{c['source']}]\n{c['text']}" for c in chunks)
    system = (
        "Answer the question using ONLY the context below. "
        "If the context is insufficient, say so briefly.\n\n"
        f"Context:\n{context}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": query}]


def _openai_response(answer: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": config.GEN_MODEL,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()

    if body.get("stream"):
        return JSONResponse(status_code=400,
                            content={"detail": "streaming not supported in v1"})

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse(status_code=400, content={"detail": "messages required"})

    query = _last_user_message(messages)
    if query is None:
        return JSONResponse(status_code=400, content={"detail": "no user message"})

    chunks, top_score = rag.retrieve(query)

    if top_score < config.get_threshold():
        stats.record(top_score, escalated=True)
        return JSONResponse(status_code=424, content=ESCALATE_BODY)

    answer = ollama.chat(_build_prompt(chunks, query))
    stats.record(top_score, escalated=False)
    return JSONResponse(status_code=200, content=_openai_response(answer))


@app.get("/health")
def health():
    ok, missing = ollama.models_ready()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "not ready", "missing_models": missing},
    )


# --- Dashboard surface (localhost-only for mutations; never exposed via gateway) ---

@app.get("/stats")
def get_stats():
    return stats.snapshot()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return _DASHBOARD.read_text()


def _is_local(req: Request) -> bool:
    return bool(req.client) and req.client.host in _LOOPBACK


@app.post("/config")
async def set_config(req: Request):
    # Config surface must not be publicly reachable — localhost bind check only.
    if not _is_local(req):
        return JSONResponse(status_code=403, content={"detail": "localhost only"})
    body = await req.json()
    try:
        value = config.set_threshold(float(body["threshold"]))
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"threshold": value}


@app.post("/stats/reset")
def reset_stats(req: Request):
    if not _is_local(req):
        return JSONResponse(status_code=403, content={"detail": "localhost only"})
    stats.reset()
    return {"status": "reset"}


@app.post("/learn")
async def learn(req: Request):
    """Gateway callback after 424→cloud: store {query,answer} if query matches LEARN_TAGS.

    Side channel — never on the chat hot path. Reachable from the gateway
    (not localhost-only). Failures here must not block the user's cloud answer.
    """
    body = await req.json()
    query, answer = body.get("query"), body.get("answer")
    if not isinstance(query, str) or not query.strip() \
            or not isinstance(answer, str) or not answer.strip():
        return JSONResponse(status_code=400, content={"detail": "query and answer required"})

    if not config.tag_match(query):
        return {"stored": False, "reason": "no tag match"}

    rag.upsert_learned(query.strip(), answer.strip())
    return {"stored": True}
