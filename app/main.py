"""FastAPI router — OpenAI-compatible /v1/chat/completions with the 424 gate.

Per request:
  0. extract last user message      -> 400 if none
  1. retrieve(query) -> top_score
  2. top_score < THRESHOLD          -> 424 {"detail": "no local context, escalate"}
  3. else prompt Qwen with context  -> 200 (JSON, or SSE if stream:true)

The 424 is the escalation signal. A response-based routing policy on the gateway
(Phase 4) rewrites it into a reroute to the cloud provider — built-in failover
ignores HTTP status (Phase-0 smoke), so the policy is mandatory.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, ollama, rag, stats
from .gateway import Conn, Provider, make_adapter

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("local0")


async def _json_or_none(req: Request):
    """Parse a JSON body, returning None on malformed input (caller → 400)."""
    try:
        return await req.json()
    except (json.JSONDecodeError, ValueError):
        return None


app = FastAPI(title="local0", version="1.0.0")
_DASHBOARD = Path(__file__).parent / "dashboard.html"
app.mount("/assets", StaticFiles(directory=Path(__file__).parent.parent / "assets"), name="assets")

ESCALATE_BODY = {"detail": "no local context, escalate"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Phrases the local model emits when the retrieved context doesn't actually
# answer the query. ponytail: substring heuristic, swap for a classifier if
# false-escalations show up in stats.
_REFUSAL_MARKERS = (
    "does not mention",
    "does not provide",
    "does not include",
    "does not contain",
    "not mentioned in the context",
    "not in the context",
    "no information",
    "context does not",
    "doesn't mention",
    "doesn't provide",
    "doesn't include",
    "doesn't contain",
    "cannot answer",
    "can't answer",
    "cannot provide an answer",
    "can't provide an answer",
    "unable to answer",
)


def _strip_think(answer: str) -> str:
    """Drop any <think>…</think> reasoning the model leaked despite think=False.

    Belt-and-suspenders for Ollama builds that ignore the think flag — the reasoning
    must never reach the client (black box) nor the refusal gate (false escalation).
    A truncated stream can leave an unterminated <think> with no closing tag; drop
    that whole tail too rather than leaking half a reasoning trace.
    """
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL)
    answer = re.sub(r"<think>.*$", "", answer, flags=re.DOTALL)  # unterminated tail
    return answer.strip()


def _is_refusal(answer: str) -> bool:
    a = answer.lower()
    return any(m in a for m in _REFUSAL_MARKERS)


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


def _openai_response(answer: str, usage: dict) -> dict:
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
        "usage": usage,
    }


def _openai_sse(answer: str):
    """OpenAI chat.completion.chunk SSE (full answer in one delta, then stop)."""
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    chunks = [
        {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": config.GEN_MODEL,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer},
                         "finish_reason": None}],
        },
        {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": config.GEN_MODEL,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    for c in chunks:
        yield f"data: {json.dumps(c)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await _json_or_none(req)
    if body is None:
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})
    # The chat path does blocking I/O (Ollama generation up to 120s, Qdrant search).
    # Run it in the threadpool so one slow request doesn't stall the whole event loop.
    return await run_in_threadpool(_handle_chat, body)


def _handle_chat(body: dict) -> Response:
    # Default to SSE. Hermes sends no `stream` flag yet force-parses the response
    # as SSE, so JSON would read as an empty stream. Only an explicit stream:false
    # opts into a JSON body.
    want_stream = bool(body.get("stream", True))

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse(status_code=400, content={"detail": "messages required"})

    query = _last_user_message(messages)
    if query is None:
        return JSONResponse(status_code=400, content={"detail": "no user message"})

    # Keyword gate: a query outside our local scope (LEARN_TAGS set but no match)
    # escalates straight to cloud without a local attempt. /learn's tag_match then
    # also refuses to store it, so an off-topic query is never learned. Empty tags =
    # unscoped, answer everything locally (old behaviour).
    if config.get_learn_tags() and not config.tag_match(query):
        stats.record(0.0, escalated=True)
        return JSONResponse(status_code=424, content=ESCALATE_BODY)

    # Fail-open: an Ollama or Qdrant outage escalates to cloud (424) rather than
    # 500ing the client. Availability first — the whole point of the router is that
    # a weak/broken local path reroutes, and "broken" includes infra down.
    try:
        chunks, top_score = rag.retrieve(query)
    except Exception:
        log.exception("retrieval failed; escalating")
        stats.record(0.0, escalated=True)
        return JSONResponse(status_code=424, content=ESCALATE_BODY)

    thr = config.get_threshold()
    if top_score < thr:  # answer not found: retrieval too weak
        stats.record(top_score, escalated=True)
        return JSONResponse(status_code=424, content=ESCALATE_BODY)

    try:
        raw, usage = ollama.chat(_build_prompt(chunks, query))
    except Exception:
        log.exception("local generation failed; escalating")
        stats.record(top_score, escalated=True)
        return JSONResponse(status_code=424, content=ESCALATE_BODY)

    answer = _strip_think(raw)
    # Retrieval score alone can't tell "topic-adjacent but answer-absent" (~0.71)
    # from a real hit (~0.77). If the local model says it can't answer from context
    # (or returns nothing once reasoning is stripped), treat that as answer-not-found.
    if not answer or _is_refusal(answer):
        stats.record(top_score, escalated=True)
        return JSONResponse(status_code=424, content=ESCALATE_BODY)
    stats.record(top_score, escalated=False)
    if want_stream:
        return StreamingResponse(
            _openai_sse(answer),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    return JSONResponse(status_code=200, content=_openai_response(answer, usage))


@app.get("/health")
def health():
    ok, missing = ollama.models_ready()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "not ready", "missing_models": missing},
    )


# --- Dashboard surface (local-network only for mutations; never exposed via gateway) ---

@app.get("/stats")
def get_stats():
    return stats.snapshot()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return _DASHBOARD.read_text()


@app.get("/learned")
def get_learned():
    """Cached Q/A entries currently in Qdrant (source=learn)."""
    return {"items": rag.list_learned()}


@app.get("/debug")
def debug():
    """Diagnostics for the dashboard: is Qdrant reachable, how many points, and
    has the gateway's 424→/learn callback ever actually fired. escalated>0 with
    learn_calls==0 means the gateway never called back (broken reroute policy)."""
    s = stats.snapshot()
    return {"qdrant": rag.qdrant_status(),
            "escalated": s["escalated"], "learn_calls": s["learn_calls"]}


def _is_local(req: Request) -> bool:
    """Allow the localhost dashboard; block the gateway and sibling containers.

    The dashboard is opened at localhost:8081, so its XHRs carry Host: localhost.
    The gateway / other containers address the router as router-service:8081, so
    the Host header is the reliable local signal — Docker Desktop NATs published
    traffic through a bridge gateway IP that isn't loopback or RFC-1918 private,
    which made the source-IP check reject the browser (172.64.x.x).
    ponytail: Host is client-settable; fine for a PoC config surface, tighten if
    this ever guards anything sensitive.
    """
    hostname = (req.headers.get("host") or "").split(":")[0]
    if hostname in ("localhost", "127.0.0.1", "[::1]", "::1"):
        return True
    if not req.client:
        return False
    host = req.client.host
    if host in _LOOPBACK:
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_private
    except ValueError:
        return False


@app.post("/config")
async def set_config(req: Request):
    # Config surface must not be publicly reachable — local/private network only.
    if not _is_local(req):
        return JSONResponse(status_code=403, content={"detail": "local access only"})
    body = await _json_or_none(req)
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})
    out: dict = {}
    try:
        if "threshold" in body:
            out["threshold"] = config.set_threshold(float(body["threshold"]))
        if "tags" in body:
            out["tags"] = config.set_learn_tags(body["tags"])
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    if not out:
        return JSONResponse(status_code=400, content={"detail": "threshold or tags required"})
    return out


@app.post("/stats/reset")
def reset_stats(req: Request):
    if not _is_local(req):
        return JSONResponse(status_code=403, content={"detail": "local access only"})
    stats.reset()
    return {"status": "reset"}


@app.post("/demo/gateway-chat")
async def demo_gateway_chat(req: Request):
    """Dashboard button: POST through the live gateway /local0/ path (full escalate+learn)."""
    if not _is_local(req):
        return JSONResponse(status_code=403, content={"detail": "local access only"})
    try:
        body = await req.json()
    except Exception:
        body = {}
    query = body.get("query") if isinstance(body, dict) else None
    if not isinstance(query, str) or not query.strip():
        query = "Does Gravitee endorse pineapple on pizza?"
    url = config.GATEWAY_CHAT_URL
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                url,
                json={"messages": [{"role": "user", "content": query.strip()}]},
            )
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"detail": f"gateway unreachable: {e}", "url": url})
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": r.text[:500]}
    model = None
    if isinstance(payload, dict):
        model = payload.get("model")
    return {
        "url": url,
        "status_code": r.status_code,
        "model": model,
        "query": query.strip(),
    }


def _conn_from(body: dict) -> Conn:
    return Conn(
        mapi_base=body["mapi_base"].rstrip("/"),
        org_id=body.get("org_id") or "DEFAULT",
        env_id=body.get("env_id") or "DEFAULT",
        token=body.get("token") or None,
        user=body.get("user") or None,
        password=body.get("password") or None,
    )


@app.post("/gateway/test")
async def gateway_test(req: Request):
    """Dashboard 'Test connection' → GatewayAdapter.test_connection. Secrets pass-through, never logged."""
    if not _is_local(req):
        return JSONResponse(status_code=403, content={"detail": "local access only"})
    body = await _json_or_none(req)
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})
    try:
        conn = _conn_from(body)
    except (KeyError, AttributeError):
        return JSONResponse(status_code=400, content={"detail": "mapi_base required"})
    return {"ok": make_adapter("gravitee").test_connection(conn)}


@app.post("/gateway/models")
async def gateway_models(req: Request):
    """Dashboard fallback picker → APIs already registered in the gateway.

    Public path is turned into a callout base_url client-side (gateway origin +
    path), so the operator picks an existing provider instead of retyping it."""
    if not _is_local(req):
        return JSONResponse(status_code=403, content={"detail": "local access only"})
    body = await _json_or_none(req)
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})
    try:
        conn = _conn_from(body)
    except (KeyError, AttributeError):
        return JSONResponse(status_code=400, content={"detail": "mapi_base required"})
    try:
        return {"models": make_adapter("gravitee").list_models(conn)}
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"detail": f"list failed: {e}"})


@app.post("/gateway/deploy")
async def gateway_deploy(req: Request):
    """Dashboard 'Deploy' → push router #1 + big-model #2 + 424-reroute policy to the gateway."""
    if not _is_local(req):
        return JSONResponse(status_code=403, content={"detail": "local access only"})
    body = await _json_or_none(req)
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})
    try:
        conn = _conn_from(body)
        fb = body["fallback"]
        provider = Provider(
            name=fb.get("name") or "big-model",
            base_url=fb["base_url"],
            api_key=fb["api_key"],
            model=fb.get("model") or "",
        )
        router_url = body["router_url"]
    except (KeyError, TypeError, AttributeError) as e:
        return JSONResponse(status_code=400, content={"detail": f"missing field: {e}"})
    # Both URLs get baked into the gateway API definition (router endpoint target +
    # cloud http-callout). An empty/relative value ships a malformed callout URL that
    # 500s the gateway on every escalation ("no protocol: /chat/completions"), so
    # reject it here at the trust boundary instead of deploying a broken API.
    for field, url in (("router_url", router_url), ("fallback.base_url", provider.base_url)):
        if not re.match(r"^https?://", url or ""):
            return JSONResponse(status_code=400, content={
                "detail": f"{field} must be an absolute http(s) URL (got {url!r})"})
    try:
        api_id, path = make_adapter("gravitee").deploy_router(conn, router_url, provider)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"detail": f"deploy failed: {e}"})
    return {
        "api_id": api_id,
        "path": path,
        "url": f"http://localhost:8082{path}v1/chat/completions",
    }


def _openai_content(obj) -> str | None:
    """Pull assistant text out of an OpenAI chat.completion object (or None)."""
    try:
        return obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def _answer_from_sse(text: str) -> str | None:
    """Assemble assistant text from an OpenAI SSE payload (data: ... lines)."""
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        try:
            choice = obj["choices"][0]
        except (KeyError, IndexError, TypeError):
            continue
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str):
            parts.append(delta["content"])
        msg = choice.get("message") or {}
        if isinstance(msg.get("content"), str):
            return msg["content"]
    return "".join(parts) or None


def _recover_learn_from_raw(raw: str) -> tuple[str | None, str | None]:
    """Best-effort query/answer when gateway inlined SSE and broke JSON.parse."""
    answer = _answer_from_sse(raw)
    query = None
    # Last "role":"user","content":"..." in the request fragment.
    for m in re.finditer(
        r'"role"\s*:\s*"user"\s*,\s*"content"\s*:\s*"((?:\\.|[^"\\])*)"', raw
    ):
        try:
            query = json.loads(f'"{m.group(1)}"')
        except json.JSONDecodeError:
            query = m.group(1)
    return query, answer


@app.post("/learn")
async def learn(req: Request):
    """Gateway callback after 424→cloud: cache {query,answer} if query matches LEARN_TAGS.

    Accepts either the explicit `{query, answer}` (dashboard Test /learn) OR the
    raw gateway forward `{request, completion}` — the original OpenAI request and
    the cloud's chat.completion — so the 424-reroute policy can post both verbatim
    with no JSON surgery in the gateway. Side channel, never on the chat hot path;
    failures here must not block the user's cloud answer.
    """
    stats.record_learn_call()
    raw = (await req.body()).decode("utf-8", errors="replace")
    body: dict | None
    try:
        parsed = json.loads(raw)
        body = parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        body = None

    query = answer = None
    if body is not None:
        query, answer = body.get("query"), body.get("answer")
        if not query and isinstance(body.get("request"), dict):
            query = _last_user_message(body["request"].get("messages") or [])
        if not answer and isinstance(body.get("completion"), dict):
            answer = _openai_content(body["completion"])
        elif not answer and isinstance(body.get("completion"), str):
            answer = _answer_from_sse(body["completion"]) or body["completion"]
    else:
        query, answer = _recover_learn_from_raw(raw)

    if not isinstance(query, str) or not query.strip() \
            or not isinstance(answer, str) or not answer.strip():
        return JSONResponse(status_code=400, content={"detail": "query and answer required"})

    if not config.tag_match(query):
        return {"stored": False, "reason": "no tag match"}

    rag.upsert_learned(query.strip(), answer.strip())
    stats.record_learned()
    return {"stored": True}
