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
    def deploy_router(self, conn: Conn, router_url: str, fallback: Provider) -> str: ...
    def undeploy(self, conn: Conn, api_id: str) -> None: ...


def make_adapter(gateway_type: str) -> GatewayAdapter:
    if gateway_type == "gravitee":
        return GraviteeAdapter()
    raise ValueError(f"unsupported gateway_type: {gateway_type}")


class GraviteeAdapter:
    """Gravitee APIM v4 Management API adapter.

    Deploy sequence (a bare create is NOT a live route):
      1. POST /apis/import   — API definition JSON (embeds both providers + the
                               424 response-policy) -> api_id
      2. POST /apis/{id}/plans  — KEY_LESS plan, then publish it
      3. POST /apis/{id}/deployments  — push to the gateway
      4. POST /apis/{id}/_start
    """

    def _base(self, conn: Conn) -> str:
        return f"{conn.mapi_base}/organizations/{conn.org_id}/environments/{conn.env_id}"

    def _client(self, conn: Conn) -> httpx.Client:
        return httpx.Client(headers=conn.headers(), auth=conn.auth(), timeout=30.0)

    def test_connection(self, conn: Conn) -> bool:
        try:
            with self._client(conn) as c:
                r = c.get(f"{self._base(conn)}/apis")
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    def deploy_router(self, conn: Conn, router_url: str, fallback: Provider) -> str:
        definition = build_api_definition(router_url, fallback)
        base = self._base(conn)
        with self._client(conn) as c:
            r = c.post(f"{base}/apis/import", json=definition)
            r.raise_for_status()
            api_id = r.json()["id"]

            plan = {
                "name": "keyless",
                "definitionVersion": "V4",
                "security": {"type": "KEY_LESS", "configuration": {}},
                "status": "PUBLISHED",
            }
            c.post(f"{base}/apis/{api_id}/plans", json=plan).raise_for_status()
            c.post(f"{base}/apis/{api_id}/deployments", json={}).raise_for_status()
            c.post(f"{base}/apis/{api_id}/_start").raise_for_status()
        return api_id

    def undeploy(self, conn: Conn, api_id: str) -> None:
        base = self._base(conn)
        with self._client(conn) as c:
            c.post(f"{base}/apis/{api_id}/_stop")
            c.delete(f"{base}/apis/{api_id}")


def build_api_definition(router_url: str, fallback: Provider) -> dict:
    """V4 proxy API: router as endpoint #1, big model as #2, plus a response-phase
    policy that reroutes to #2 when the router replies 424.

    Copy the running Hermes LLM Proxy import
    (gravitee-io-labs/Gravitee-AI-Agent-Workshop
    `Hermes-LLMs-1-0.json`) as the concrete payload template —
    endpoint-group shape and policy plugin id come from that live example.
    This is the structural skeleton; verify plugin ids against the target
    APIM before first deploy.

    Phase-0.5 open item: confirm Gravitee forwards the ORIGINAL user messages to
    the fallback on reroute (not the RAG-augmented body). If augmented-only, add a
    transform/passthrough step to the escalate flow here.
    """
    return {
        "definitionVersion": "V4",
        "type": "PROXY",
        "name": "smart-local-router",
        "listeners": [{
            "type": "HTTP",
            "paths": [{"path": "/router/"}],
            "entrypoints": [{"type": "http-proxy"}],
        }],
        "endpointGroups": [{
            "name": "local-first",
            "type": "http-proxy",
            "endpoints": [
                {"name": "router", "type": "http-proxy",
                 "weight": 1, "configuration": {"target": router_url}},
                {"name": "big-model", "type": "http-proxy", "secondary": True,
                 "configuration": {
                     "target": fallback.base_url,
                     "headers": {"Authorization": f"Bearer {fallback.api_key}"}}},
            ],
        }],
        "flows": [{
            "name": "escalate-on-424",
            "selectors": [{"type": "HTTP", "path": "/", "pathOperator": "STARTS_WITH"}],
            "response": [{
                "name": "reroute-on-424",
                "policy": "policy-assign-attributes",
                "condition": "{#response.status == 424}",
                "configuration": {
                    "attributes": [
                        {"name": "gravitee.attribute.endpoint", "value": "big-model"},
                    ],
                },
            }],
        }],
    }
