"""Gateway adapter — pushes the router's escalation wiring to an LLM gateway.

The whole escalation mechanism lives in the deployed API definition: BOTH
providers (router #1, big model #2) AND a response-based routing policy that
reroutes on upstream 424. deploy_router owns all of it — if the adapter only
registered the router endpoint, an operator would hand-wire the policy and the
"zero console clicks" goal fails.

ponytail: one impl (Gravitee) behind a tiny interface. No plugin registry until
a second gateway exists — the `if gateway_type == "gravitee"` seam is enough.

Secrets (MAPI token, big-model creds) are pass-through from the UI and are NEVER
logged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass
class Conn:
    mapi_base: str          # e.g. http://gravitee-mgmt:8083/management
    org_id: str
    env_id: str
    token: str | None = None            # bearer, OR
    user: str | None = None
    password: str | None = None

    def auth(self) -> httpx.Auth | None:
        if self.user is not None:
            return httpx.BasicAuth(self.user, self.password or "")
        return None

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


@dataclass
class Provider:
    """Big-model fallback (provider #2). creds pass-through, never logged."""
    name: str
    base_url: str
    api_key: str
    model: str


class GatewayAdapter(Protocol):
    def test_connection(self, conn: Conn) -> bool: ...
    def deploy_router(self, conn: Conn, router_url: str, fallback: Provider) -> tuple[str, str]: ...
    def undeploy(self, conn: Conn, api_id: str) -> None: ...


def make_adapter(gateway_type: str) -> GatewayAdapter:
    if gateway_type == "gravitee":
        return GraviteeAdapter()
    raise ValueError(f"unsupported gateway_type: {gateway_type}")


class GraviteeAdapter:
    """Gravitee APIM v4 Management API adapter.

    Targets the v4 management API (`/management/v2/environments/{env}`), which is
    env-scoped (no org segment) and expects the `{api, plans}` import envelope.
    The legacy `/organizations/{org}/environments/{env}/apis/import` path 500s on a
    V4 body ("getProxy() is null") — it only accepts v2 definitions.

    Deploy sequence (verified against live APIM v4, matches the workshop's own
    apim_init.py):
      1. POST /apis/_import/definition  — {api, plans} envelope -> api_id
      2. GET/PUT /apis/{id}             — set lifecycleState=PUBLISHED
      3. POST /apis/{id}/_start         — push live to the gateway
    """

    def _base(self, conn: Conn) -> str:
        # v4 management API is environment-scoped; org_id is unused here.
        return f"{conn.mapi_base}/v2/environments/{conn.env_id}"

    def _client(self, conn: Conn) -> httpx.Client:
        return httpx.Client(headers=conn.headers(), auth=conn.auth(), timeout=30.0)

    def test_connection(self, conn: Conn) -> bool:
        try:
            with self._client(conn) as c:
                r = c.get(f"{self._base(conn)}/apis", params={"page": 1, "perPage": 1})
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    def deploy_router(self, conn: Conn, router_url: str, fallback: Provider) -> tuple[str, str]:
        envelope, path = build_api_definition(router_url, fallback)
        base = self._base(conn)
        with self._client(conn) as c:
            # Drop prior local0 deploys so the fixed /local0/ path is free and
            # stale /router-*/ APIs don't confuse operators.
            self._purge_prior_routers(c, base)
            r = c.post(f"{base}/apis/_import/definition", json=envelope)
            r.raise_for_status()
            api_id = r.json()["id"]

            # Publish: read the created API back, flip lifecycleState, PUT it.
            api = c.get(f"{base}/apis/{api_id}")
            api.raise_for_status()
            body = api.json()
            body["lifecycleState"] = "PUBLISHED"
            c.put(f"{base}/apis/{api_id}", json=body).raise_for_status()

            c.post(f"{base}/apis/{api_id}/_start").raise_for_status()
        return api_id, path

    def _purge_prior_routers(self, c: httpx.Client, base: str) -> None:
        """Stop+delete prior smart-local-router / /local0/ APIs (best-effort)."""
        listed = c.get(f"{base}/apis", params={"page": 1, "perPage": 100})
        if listed.status_code >= 400:
            return
        for a in listed.json().get("data", []):
            name = a.get("name") or ""
            paths = [
                p.get("path", "")
                for lis in (a.get("listeners") or [])
                for p in (lis.get("paths") or [])
            ]
            if not (name.startswith("smart-local-router") or "/local0/" in paths):
                continue
            api_id = a.get("id")
            if not api_id:
                continue
            c.post(f"{base}/apis/{api_id}/_stop")
            plans = c.get(f"{base}/apis/{api_id}/plans")
            if plans.status_code < 400:
                for p in plans.json().get("data", []):
                    c.post(f"{base}/apis/{api_id}/plans/{p['id']}/_close")
            c.delete(f"{base}/apis/{api_id}")

    def undeploy(self, conn: Conn, api_id: str) -> None:
        # Stop, close every plan, then delete — DELETE 400s while a plan is open.
        base = self._base(conn)
        with self._client(conn) as c:
            c.post(f"{base}/apis/{api_id}/_stop")
            plans = c.get(f"{base}/apis/{api_id}/plans")
            if plans.status_code < 400:
                for p in plans.json().get("data", []):
                    c.post(f"{base}/apis/{api_id}/plans/{p['id']}/_close")
            c.delete(f"{base}/apis/{api_id}")


def _escalate_flow(router_url: str, fallback: Provider) -> dict:
    """The 424→reroute mechanism, embedded as a response-phase flow.

    Verified end-to-end against live APIM v4 (weak-retrieval query → gateway
    returns the cloud answer with HTTP 200). The pieces, and why each is what it
    is (all learned the hard way against the live gateway):

      request phase:
        - assign-attributes captures the ORIGINAL request body into `origBody`.
          Reading {#request.content} in the RESPONSE phase deadlocks (the body is
          already consumed), so it must be grabbed up front.
      response phase, all gated on {#response.status == 424}:
        1. http-callout POSTs `origBody` to the cloud model, storing the cloud
           response body in `cloudAnswer`.
        2. assign-content replaces the 424 body with `cloudAnswer`. Its body
           field is FREEMARKER (${...}), NOT Gravitee EL ({#...}) — EL comes out
           literal here.
        3. status-code remaps 424→200 (a dedicated policy: the groovy sandbox
           blocks response.status assignment).

    A strong-retrieval query returns 200 from the router and skips every gated
    policy, so local answers pass through untouched.
    """
    callout_url = f"{fallback.base_url.rstrip('/')}/chat/completions"
    learn_url = f"{router_url.rstrip('/')}/learn"
    on_424 = "{#response.status == 424}"
    headers = [{"name": "Content-Type", "value": "application/json"}]
    if fallback.api_key:
        headers.append({"name": "Authorization", "value": f"Bearer {fallback.api_key}"})
    # Hermes (and most OpenAI-compat upstreams) require `model`. Clients often omit
    # it; forwarding origBody verbatim then yields an error JSON with no
    # choices[0].message.content, so /learn gets answer_len=null and stores nothing.
    #
    # Force stream:false to the cloud: policy-http-callout BUFFERS the response (it
    # can't proxy a stream), and the /hermes-llm upstream aggregates into one JSON
    # anyway — so requesting a stream buys nothing. We take that buffered
    # chat.completion and re-emit it as SSE ourselves in swap-body below, because the
    # client (Hermes) parses this reroute response as SSE (a bare JSON object reads to
    # it as "empty stream with no finish_reason"). Hermes selects the parser by
    # Content-Type, so force-sse-ct below sets it to text/event-stream.
    #
    # Body MUST be a single Gravitee EL expression starting with `{#...}` — a raw
    # JSON template mixed with `{#jsonPath...}` 500s the gateway. Inject model+stream
    # by string-replacing `"messages"` inside the captured request body.
    #
    # Hermes DOES send "stream":true (the OpenAI SDK adds it when streaming). Our
    # injected "stream":false lands before "messages", but the client's later
    # "stream":true wins on duplicate JSON keys → the cloud streams SSE → extract-answer's
    # jsonPath finds no $.choices[0].message and answerText comes out empty (blank reroute).
    # Neutralize the client's flag to false first (both spacings), then inject ours.
    #
    # Also force tool_choice:"none". Hermes is an agent — it passes its whole toolset
    # (web_search, etc.), and the cloud model answers agentically: finish_reason
    # "tool_calls", message.content "" (the text is a tool call, not prose). This
    # escalation is a RAG *text* fallback, not an agent loop, so tell the cloud to answer
    # directly. Hermes sends no tool_choice key, so this injected one is unambiguous.
    #
    # And DROP stream_options. Hermes's OpenAI SDK adds `stream_options:{include_usage:true}`
    # for its streaming request; once we force stream:false the upstream rejects that combo
    # and returns a MALFORMED completion (object:"" created:0, no message) — jsonPath extract
    # gets nothing → blank reroute. It only shows up with the real SDK (a plain-curl repro
    # lacks stream_options). Remove the key literally, both SDK-spaced and compact forms,
    # comma-before or comma-after so the remaining JSON stays valid wherever it sat.
    model = (fallback.model or "").strip()
    inj = (('"model":' + json.dumps(model) + ',' if model else "")
           + '"stream":false,"tool_choice":"none","messages"')
    cloud_body = (
        "{#context.attributes['origBody']"
        ".replace('\"stream_options\": {\"include_usage\": true}, ', '')"
        ".replace(', \"stream_options\": {\"include_usage\": true}', '')"
        ".replace('\"stream_options\":{\"include_usage\":true},', '')"
        ".replace(',\"stream_options\":{\"include_usage\":true}', '')"
        ".replace('\"stream\": true', '\"stream\":false')"
        ".replace('\"stream\":true', '\"stream\":false')"
        ".replace('\"messages\"', '" + inj + "')}"
    )
    return {
        "name": "escalate-on-424",
        "selectors": [{"type": "HTTP", "path": "/", "pathOperator": "STARTS_WITH"}],
        "request": [{
            "name": "capture-request", "enabled": True, "policy": "policy-assign-attributes",
            "configuration": {"scope": "REQUEST",
                              "attributes": [{"name": "origBody", "value": "{#request.content}"}]},
        }],
        "response": [
            {"name": "call-cloud", "enabled": True, "policy": "policy-http-callout", "condition": on_424,
             "configuration": {
                 "scope": "RESPONSE", "method": "POST", "url": callout_url, "headers": headers,
                 "body": cloud_body,
                 "variables": [{"name": "cloudAnswer", "value": "{#calloutResponse.content}"}],
                 "exitOnError": False}},
            # Pull the assistant text out of the buffered cloud JSON so swap-body can
            # re-wrap it as SSE. EL {#jsonPath} is fine as an assign-attributes value;
            # it only 500s when mixed into a raw JSON template (why swap-body is FreeMarker).
            {"name": "extract-answer", "enabled": True, "policy": "policy-assign-attributes", "condition": on_424,
             "configuration": {"scope": "RESPONSE", "attributes": [{"name": "answerText",
                 "value": "{#jsonPath(#context.attributes['cloudAnswer'], '$.choices[0].message.content')}"}]}},
            # Re-emit the buffered answer as an OpenAI SSE stream. Two chunks, matching
            # main.py _openai_sse and the OpenAI streaming convention: chunk 1 carries
            # role+content with finish_reason null, chunk 2 is an empty delta with
            # finish_reason "stop", then [DONE]. Packing content AND finish_reason into one
            # chunk makes Hermes treat it as a terminal frame and drop the content (empty
            # response). force-sse-ct sets the Content-Type that selects the SSE parser.
            # Body is FreeMarker ${...}, NOT EL {#...} (EL comes out literal); ?json_string
            # escapes the content for the JSON data: line.
            {"name": "swap-body", "enabled": True, "policy": "policy-assign-content", "condition": on_424,
             "configuration": {"scope": "RESPONSE", "body":
                 "data: {\"id\":\"chatcmpl-reroute\",\"object\":\"chat.completion.chunk\","
                 "\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\","
                 "\"content\":\"${(context.attributes['answerText']!'')?json_string}\"},"
                 "\"finish_reason\":null}]}\n\n"
                 "data: {\"id\":\"chatcmpl-reroute\",\"object\":\"chat.completion.chunk\","
                 "\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n"
                 "data: [DONE]\n\n"}},
            # Fire-and-forget cache-back: post the original request + cloud completion to
            # the router's /learn, which caches into Qdrant iff the query matches a learn
            # tag. cloudAnswer is a plain chat.completion JSON, so it inlines cleanly.
            # exitOnError False → a /learn hiccup never blocks the user's answer.
            {"name": "cache-back", "enabled": True, "policy": "policy-http-callout", "condition": on_424,
             "configuration": {
                 "scope": "RESPONSE", "method": "POST", "url": learn_url,
                 "headers": [{"name": "Content-Type", "value": "application/json"}],
                 "body": "{\"request\": {#context.attributes['origBody']}, "
                         "\"completion\": {#context.attributes['cloudAnswer']}}",
                 "exitOnError": False}},
            {"name": "reroute-status", "enabled": True, "policy": "status-code",
             "configuration": {"statusMappings": [{"inputStatusCode": 424, "outputStatusCode": 200}]}},
            # Force SSE content-type on EVERY response. Hermes parses by content-type:
            # an SSE body under application/json is JSON.parse'd and reads as empty. Both
            # paths emit SSE now (router streams local answers, swap-body streams reroute),
            # so this is unconditional. addHeaders overwrites the existing value.
            {"name": "force-sse-ct", "enabled": True, "policy": "transform-headers",
             "configuration": {"scope": "RESPONSE",
                 "addHeaders": [{"name": "Content-Type", "value": "text/event-stream"}]}},
        ],
    }


def build_api_definition(router_url: str, fallback: Provider) -> tuple[dict, str]:
    """V4 PROXY API import envelope with the router endpoint + the 424→reroute flow.

    Shape verified against live APIM v4 (import 201 → publish → start) and the
    workshop's Hermes-LLMs-1-0.json. The `_import/definition` endpoint expects a
    top-level {api, plans} envelope — a flat api def 500s.

    The cloud fallback is reached by a response-phase http-callout (see
    _escalate_flow), not a second proxy endpoint, so the endpoint group holds only
    the router. Fixed path `/local0/` so the gateway URL is stable across redeploys.

    Returns (envelope, gateway_path).
    """
    path = "/local0/"
    return {
        "api": {
            "definitionVersion": "V4",
            "type": "PROXY",
            "name": "smart-local-router",
            "description": "local0 smart local router (escalates weak retrieval)",
            "apiVersion": "1.0",
            "listeners": [{
                "type": "HTTP",
                "paths": [{"path": path}],
                "entrypoints": [{"type": "http-proxy"}],
            }],
            "endpointGroups": [{
                "name": "local-first",
                "type": "http-proxy",
                "endpoints": [
                    {"name": "router", "type": "http-proxy", "weight": 1,
                     "inheritConfiguration": False,
                     "configuration": {"target": router_url}},
                ],
            }],
            "flows": [_escalate_flow(router_url, fallback)],
        },
        "plans": [{
            "definitionVersion": "V4",
            "name": "Open",
            "description": "Default keyless plan",
            "security": {"type": "KEY_LESS", "configuration": {}},
            "mode": "STANDARD",
            "status": "PUBLISHED",
            "type": "API",
            "validation": "MANUAL",
            "flows": [],
        }],
    }, path
