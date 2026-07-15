# PRD — local0 (Smart Local Router)

**Status:** PoC
**Last updated:** 2026-07-15
**Source plan:** `plans/merged-plan.md`

**Doc authority:** PRD wins on *product* (what success means, scope, goals). `merged-plan.md` wins on *deploy + phase detail* (host Ollama, `docker_default`, Hermes template, gateway `:8082`, model tag, operational gotchas). On conflict: product wording → PRD; how-to-build → plan.

---

## 1. Summary

A local-first RAG endpoint that sits **behind** an LLM gateway as a provider. A small local model (Qwen3 0.6B) answers when retrieval against a local vector DB is strong; when retrieval is weak the service emits an HTTP `424`, which a **response-based routing policy on the gateway** reroutes to a large cloud model. (Built-in failover ignores HTTP status — Phase-0 proved it — so the policy is mandatory, not a failover signal.) Ships as `docker compose up`. The gateway config is pushed from the app's own dashboard via the gateway's Management API, so the gateway is **swappable** — the PoC targets Gravitee APIM v4.

**This service owns:** local model serving, vector DB + retrieval, escalation signal, gateway-config push.
**Gateway owns:** routing, auth, semantic cache, guardrails, observability, cost tracking.

---

## 2. Goals / Non-goals

### Goals
- Answer in-corpus questions locally, on a laptop, with no GPU.
- Escalate out-of-corpus questions to a cloud model automatically. **A response-based routing policy on the gateway** rewrites upstream `424` → reroute to the cloud provider. (Phase-0 smoke proved built-in failover ignores HTTP status, so it can't do this alone — see Risk #1.) Router stays gateway-agnostic: it only returns `424`.
- Make the gateway swappable: connection + config come from a UI form and are deployed through an adapter.
- One-command run for a fresh clone.
- Replace the "60–80% local" guess with a measured, defended number.

### Non-goals (v1)
- Streaming responses (`stream:true` → `400`).
- Multi-turn / follow-up query rewriting (last user message only).
- PDF ingestion.
- Concurrency (CPU Qwen serializes requests — single-user demo).
- A second gateway adapter or plugin registry (Gravitee only for PoC).
- Rebuilding gateway-grade observability/cost dashboards (lightweight local view only).

---

## 3. Users

- **Agent / app developer** — points their agent (Claude, GPT, custom) at the gateway endpoint; unaware of local-vs-cloud routing.
- **Operator (us)** — runs `docker compose up`, connects the gateway from the dashboard, tunes the escalation threshold.

---

## 4. Build — languages & stack

| Component | Language / Tech | Notes |
|---|---|---|
| Router service | **Python 3.11 + FastAPI** | OpenAI-compatible `/v1/chat/completions` in/out |
| Ingestion + retrieval | **Python** (`ingest.py`, `retrieve()`) | naive fixed-size chunker (~500 tok, ~50 overlap) |
| Local model serving | **Ollama** (host process) | `qwen3:0.6b` (already pulled) + `nomic-embed-text`, OpenAI-compatible routes. Upgrade → 1.7b/4b only if Phase 6 eval shows weak synthesis |
| Vector DB | **Qdrant** (Docker image) | persistent volume, cosine, 768-dim |
| Gateway (PoC) | **Gravitee APIM v4** | LLM Proxy; external gateway `:8082` on `docker_default` |
| Gateway adapter | **Python** (`GatewayAdapter` + `GraviteeAdapter`) | pushes config via Management API |
| Dashboard | **Router-served HTML/JS** (no framework, no extra container) | stats view + threshold knob + gateway-connect form |
| Orchestration | **Docker Compose** | services: `vectordb`, `router` (Ollama on host) |
| Config | **`.env`** | model tag, `THRESHOLD`, collection name, big-model creds ref |

**Rationale (locked in Phase 0):**
- Escalation signal = **HTTP 424 + response-based routing policy** — Phase-0 smoke proved built-in failover ignores HTTP status, so the policy (upstream 424 → reroute to provider #2) is required.
- Confidence v1 = **retrieval top_score only** — one gate, no second model call.
- Distance = **cosine** so `THRESHOLD` sits on a **comparable** scale across embedding models ([-1,1]) — not transferable; swapping the embed model still needs a `THRESHOLD` re-eval (Phase 6). Dim pinned at **768** on collection create.
- Default `THRESHOLD` = **0.55** (pre-eval guess), replaced by a measured value in Phase 6.

---

## 5. Architecture

```
User's Agent  (Claude / GPT / custom)
      │
      ▼
Gravitee LLM Proxy (:8080 entry, gateway :8082)
   provider 1: router-service   (local-first)
   provider 2: big-model API     (policy reroute target on 424)
      │
      ▼
Router Service — FastAPI (:8081)
   1. extract query (last user message)
   2. embed (nomic-embed-text)
   3. retrieve top-k from Qdrant → top_score
   4. gate: top_score < THRESHOLD → 424 (escalate)
   5. else prompt Qwen with context → 200
      │                        │
      ▼                        ▼
   Qdrant                   Ollama (Qwen3 0.6B + nomic-embed)
```

**Topology rule:** the router sits *behind* the gateway. The router only *raises* the 424 flag; the **gateway routes**. The router never fronts the gateway.

---

## 6. Escalation contract (the one hard decision)

- Router returns `424 Failed Dependency` when `top_score < THRESHOLD`, body `{"detail": "no local context, escalate"}`. It does **not** call the model.
- **A response-based routing policy** (Phase 4) inspects the router's response; on status `424` it reroutes to provider #2 (big model).
- **Built-in failover will NOT do this** — Phase-0 smoke (2026-07-15) proved it retries only on connection/transport failure and ignores HTTP status. The policy is mandatory; without it the client gets a raw 424 and nothing escalates.
- **Open question (verify in Phase 4):** Gravitee must forward the *original* user messages to the big model, not the RAG-augmented prompt. If it only forwards the augmented body, add a passthrough branch.

---

## 7. Functional requirements

### Router `/v1/chat/completions`
- `stream:true` → `400`.
- Extract query = last `role:"user"` message content only; no user message → `400`.
- `chunks, top_score = retrieve(query)`.
- `top_score < THRESHOLD` → `424` (no model call).
- Else build prompt (context + query), call Qwen, return `200` OpenAI-compatible response.

### Ingestion / retrieval
- Walk `./docs` (md/txt), chunk, embed via Ollama, upsert to Qdrant with source metadata.
- Collection created size=768, distance=Cosine; existing collection with different dim → hard error.
- `retrieve(query) -> (chunks, top_score)`; empty/absent collection → `([], 0.0)` (escalates cleanly).

### Gateway config (Phase 4b)
- UI form collects: MAPI base URL, auth (bearer or user/pass), org id, env id, router endpoint URL, big-model provider creds.
- `GatewayAdapter` interface: `test_connection`, `deploy_router`, `undeploy`.
- `GraviteeAdapter` deploy sequence: `POST /apis/import` → `POST /apis/{id}/plans` (publish) → `POST /apis/{id}/deployments` → `POST /apis/{id}/_start`. Bare create ≠ live route.
- **The imported API definition MUST embed the 424→reroute response-policy + both providers** (router #1, big model #2). `deploy_router` owns the whole escalation mechanism, not just endpoint registration — else the operator hand-wires the policy and the zero-console-clicks metric fails.
- Pin Gravitee **v4** MAPI schema (paths/verbs differ v3 vs v4). Reuse existing Hermes import as payload template.

---

## 8. Non-functional / security

- **Secrets pass-through only** — MAPI token and big-model creds come from the UI, forwarded to the adapter, **never logged**.
- Cold-start safe: compose healthcheck gates `router`/`ingest` on both Ollama models pulled.
- Both-providers-fail path returns a **clean error**, never a leaked raw 424.
- Cost tracking distinguishes local vs escalated (`% escalated` metric).

---

## 9. Build phases

| Phase | Deliverable |
|---|---|
| 0 — Decisions | Locked decision table (§4 rationale) + 424-failover smoke test (Risk #1) passing before any 424 code. |
| 0.5 — Prompt-forward spike | Confirm gateway forwards original (not augmented) messages on 424 reroute; else adapter needs passthrough. Before any 424 code. |
| 1 — Local model serving | Ollama up; curl gets completion; router waits on models-ready. |
| 2 — Vector DB + ingestion | `ingest.py` + `retrieve()`; known-query + empty-collection tests pass. |
| 3 — Router service | 4 cases pass: 200 / 424 / 400 malformed / 400 stream. |
| 4 — Gateway wiring | Local-first + cloud-fallback end-to-end; 424→policy reroute verified; both-fail clean error. |
| 4b — App-driven config | Dashboard form → `GatewayAdapter` → live started Gravitee API **with 424→reroute policy + both providers embedded**; low-retrieval query escalates with zero console clicks; secrets pass-through. |
| 5 — Packaging | Clone → `docker compose up` → working endpoint, no GPU. |
| 6 — Eval + threshold | Defended `THRESHOLD` + re-runnable `eval.py`. |
| 7 — Dashboard | Router-served stats view + threshold knob + gateway-connect form. |
| 8 — Content (optional) | Write-up of the 424 escalation-contract decision. |

---

## 10. Success metrics

- Fresh clone → `docker compose up` → working endpoint on a laptop, no GPU.
- Measured local-answer % from Phase 6 eval (20–30 labeled queries), not a guess.
- `THRESHOLD` chosen to maximize correct routing on the eval set.
- Gateway deployable from the dashboard with zero console clicks.

---

## 11. Open risks

- **424 failover behavior — RESOLVED (Phase-0 smoke, 2026-07-15, live APIM v4).** Built-in Gravitee failover retries only on connection/transport failure; it **ignores HTTP status**, so a raw `424` passes straight to the client. Zero-policy path rejected → **the escalation requires a response-based routing policy** (on upstream `424`, route to provider #2). Goal #2 now locked to the fallback path.
- **Prompt forwarding on escalation (TOP remaining architecture risk, now 424 is resolved)** — on reroute, the big model must get the *original* user messages, not the RAG-augmented prompt. **Resolve as a pre-Phase-4 spike (Phase 0.5), before writing router 424 code** — if the gateway forwards augmented-only, the Phase-4b adapter needs a passthrough branch in the API definition. Not a Phase-4 discovery.
- **MAPI paths/verbs** — v3 vs v4 divergence; pin v4 before writing the adapter.
- **Concurrency** — CPU Qwen serializes; flagged in README, not solved in v1.
