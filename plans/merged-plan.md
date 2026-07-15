# local0 (Smart Local Router) — build plan

Local-first RAG endpoint behind Gravitee LLM Proxy. Small local model (Qwen) answers when retrieval is strong; escalates to a big cloud model when weak. Ships as `docker compose up`.

**This service owns:** local model serving, vector DB + retrieval, escalation signal, gateway-config push (Phase 4b).
**Gravitee owns:** routing, auth, semantic cache, guardrails, observability, cost tracking.

> **Deployment reality (PoC against a local APIM stack)**
> - Gravitee is **APIM v4**; gateway typically on **:8082** (not the entry/UI port). Attach to that stack's Docker network (often named `docker_default` when using the stock workshop compose — treat the name as an env-specific fact, not a hardcoded contract).
> - Register `router-service` the same way any other LLM-proxy upstream is registered: import/publish an API definition that points at `http://router-service:8081`.
> - If the stack already runs Redis for Gravitee semantic cache, reuse it — nothing to add in this repo.
> - **Ollama already runs on the host** (:11434, native process, not a container). See Phase 1 decision.
> - Gateway → router is **container-to-container DNS** (`http://router-service:8081`), not `localhost`. Router + Qdrant must join the gateway's external Docker network or the gateway can't reach them.
> - Confirm the APIM gateway is healthy/serving before Phase 4.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     User's Agent                          │
│         (Claude, GPT, local agent, custom app)            │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│      Gravitee LLM Proxy (:8080 entry, gateway :8082)      │
│  Provider list, ordered:                                  │
│   1. router-service  (local-first)                        │
│   2. big-model API   (reroute target on 424)              │
│  Policy: on 424 from #1 → route request to #2             │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│          Router Service — FastAPI (localhost:8081)        │
│  1. Extract query from messages                           │
│  2. Embed query (local embedding model)                   │
│  3. Retrieve top-k from Qdrant → top_score                │
│  4. Gate: top_score < THRESHOLD  → return 424 (escalate)  │
│  5. Else: prompt Qwen with context → return 200 answer    │
└────┬────────────────────────────────────┬────────────────┘
     ▼                                     ▼
┌──────────┐                     ┌──────────────────────────┐
│  Qdrant  │                     │         Ollama           │
│ VectorDB │                     │ Qwen3 0.6B +  nomic-embed │
└──────────┘                     └──────────────────────────┘
```

---

## Phase 0 — Decisions locked

| Decision | Choice | Why |
|---|---|---|
| Local runtime | **Ollama** | OpenAI-compatible routes, handles model pulls, zero setup |
| Vector DB | **Qdrant** | official Docker image, persistent volume |
| Small model | **Qwen3 0.6B** (already on host) | zero pull, CPU-fast; answers are grounded in retrieved context so small is fine. Upgrade → qwen3:1.7b/4b only if Phase 6 eval shows weak synthesis |
| Embedding | **nomic-embed-text** (Ollama) | local, no cloud embed API |
| Escalation signal | **HTTP 424 + response-based routing policy** | Phase-0 smoke (2026-07-15, live APIM v4) **proved built-in failover ignores HTTP status** — retries only on connection/transport failure. Raw 424 passes to client. Zero-policy path REJECTED. Router still returns 424; a response-phase policy on the gateway converts upstream 424 → reroute to provider #2. |
| Confidence v1 | **retrieval top_score only** | one gate, cheap. No 2nd model call. |
| Distance metric | **cosine** | bounds score to [-1,1] so THRESHOLD is *comparable* across models (same scale) — NOT transferable; swap the embed model → re-eval 0.55 (Phase 6 rerun-on-swap) |
| Embedding dim | **768** (nomic-embed-text) | pinned at collection create; ingest asserts it |
| Default THRESHOLD | **0.55** (pre-eval guess) | ships in `.env.example`; Phase 6 replaces with defended value |
| Streaming | **rejected in v1** | `stream:true` → 400. Router returns single body only. |

**Escalation contract — the one hard decision:**
- Router returns `424 Failed Dependency` when `top_score < THRESHOLD`.
- A **response-based routing policy** on the gateway rewrites upstream `424` → reroute to the next provider (big model). Built-in failover won't (Phase-0 proved it ignores HTTP status).
- **Open question to verify in Phase 4:** Gravitee forwards the *original* user messages to the big model, NOT the RAG-augmented prompt. Confirm this. If Gravitee only forwards augmented body, add a passthrough branch instead.

**Deliverable:** this table filled + confirmed. **424-failover smoke DONE (2026-07-15) — result below.**

**Phase-0 smoke result (RAN, live APIM v4 gateway :8082, MAPI :8083):**
- Built a throwaway v4 PROXY API, one endpoint group, `failover.enabled=true`, 2 endpoints: ep1 + ep2.
- **ep1 returns 424 → client gets alternating 424/200 (round-robin, no retry).** Failover does NOT treat a 424 (or any HTTP status) as a failure.
- **ep1 = dead host (connection refused) → all 200.** Failover DOES retry on transport/connection failure. Mechanism is alive; it just ignores status codes.
- **Verdict: zero-custom-policy path REJECTED.** Must add a **response-based routing policy** that inspects upstream status `== 424` and forces the reroute to provider #2 (built-in failover won't do it). This is the Phase-4 wiring task.

**Phase-0.5 spike — prompt-forward (PULLED FORWARD, do before writing router 424 code):** on a 424 reroute, does the gateway forward the *original* user messages or the RAG-augmented body to provider #2? This gates whether the Phase-4b API definition needs a passthrough branch. If augmented-only, the adapter design changes — so resolve it as a small live-APIM spike now, NOT as a Phase-4 discovery. Reuse the smoke's throwaway-API method.

---

## Phase 1 — Local model serving
- **LOCKED — reuse host Ollama (:11434).** Router/ingest reach it via `http://host.docker.internal:11434`. No `llm` compose service. Document a containerized-Ollama fallback in the README for the clean-clone story, but do not build it in v1. `ponytail:` fewest moving parts.
- **Models:** `qwen3:0.6b` **already pulled** (generation). Pull once: `ollama pull nomic-embed-text` (embedding — no host alternative). glm-4.7-flash (19GB) is on host but ignored — too big for CPU.
- **Model readiness (load-bearing):** Ollama is host-side, so compose can't `depends_on` it. Two guards: (1) `make demo` / setup runs `ollama pull nomic-embed-text` **before** `docker compose up`; (2) router does a startup ping to `host.docker.internal:11434/api/tags` and asserts `qwen3:0.6b` + `nomic-embed-text` present — fail fast with a clear message, not mid-request 500s.
- Smoke: `curl localhost:11434/v1/chat/completions` (host) returns OpenAI-shaped completion.

**Deliverable:** host Ollama serving both models (no `llm` compose service); curl gets completion; router does not start until models ready.

---

## Phase 2 — Vector DB + ingestion  *(load-bearing — do it for real)*
- Compose service `vectordb` = Qdrant, persistent volume, collection name from `.env`.
- Collection created with **size=768, distance=Cosine**. If collection exists with different dim → hard error, don't silently upsert. (nomic-embed = 768; mismatch = broken search.)
- **Ingestion script** `ingest.py`:
  1. Walk a docs dir (`./docs`, md/txt — `ponytail:` PDF dropped from v1, add `pypdf` branch only if a demo doc needs it).
  2. Chunk (fixed ~500 tokens, ~50 overlap — `ponytail:` naive splitter, upgrade to semantic chunking if retrieval eval is poor).
  3. Embed each chunk via Ollama `nomic-embed-text`.
  4. Upsert to Qdrant with source metadata.
- Retrieval helper `retrieve(query) -> (chunks, top_score)`. **Empty/absent collection → return `([], 0.0)`** so a fresh clone escalates cleanly instead of crashing.

**Check (runnable):** ingest a known doc, query for content in it → assert `top_score` high; query for unrelated content → assert `top_score` low; query against empty collection → returns `0.0`, no exception.

**Deliverable:** `ingest.py` + `retrieve()`; known-query test + empty-collection test pass.

---

## Phase 3 — Router service (the product)
FastAPI app exposing `/v1/chat/completions`, OpenAI-compatible in/out.

Per request:
0. If `stream:true` → return `400` (v1 unsupported, declared in Phase 0).
1. **Extract query:** embed the **last `role:"user"` message content only** (`ponytail:` naive, ignores multi-turn context — upgrade to last-N-turn concat only if eval shows follow-up questions misroute). System/assistant messages ignored for retrieval. No user message → `400`.
2. `chunks, top_score = retrieve(query)`.
3. **Gate:** `top_score < THRESHOLD` → return `424`, minimal body `{"detail": "no local context, escalate"}`. Do NOT call the model. (Empty collection → `top_score=0.0` → always escalates, correct behavior.)
4. Else build prompt (context + query), call Qwen, return `200` OpenAI-compatible response.

`ponytail:` no answerability gate (dropped from claude-plan — doubled latency for unproven gain). Add a 2nd classifier call ONLY if Phase 6 eval shows retrieval score alone misroutes.

`ponytail:` CPU Qwen serializes requests — fine for demo, single-user. Flag concurrency limit in README; not solved in v1.

**Check (4 cases):** good local answer → 200; low retrieval → 424; malformed request → 400; `stream:true` → 400.

**Deliverable:** router service passing the 4 cases.

---

## Phase 4 — Gateway wiring (adapter-driven, Gravitee = PoC impl)

**Topology:** router sits *behind* the gateway as provider #1. Agent → Gateway → Router (200 local) / Big model (on 424). Router only *raises* the 424 escalation flag; the **gateway routes**. Router never fronts the gateway.

**Entry gate (blocks Phase 4):** confirm the APIM gateway container is healthy and serving a known route (e.g. `curl :8082/<known-api>` → non-404). A stopped/unhealthy gateway silently 404s every route and Phase 4 stalls with no error. Verify before wiring, not during.

- **Target = APIM gateway (typically :8082) on the stack's Docker network.** Attach router + vectordb to that external network. Copy an existing LLM-proxy API import from your APIM stack as the template — same import path, swap upstream to `http://router-service:8081`.
- Register `router-service` as OpenAI-compatible LLM Proxy endpoint (priority 1).
- Register big model (Anthropic/OpenAI/Bedrock) as endpoint (priority 2).
- **Prompt-forward already resolved in the Phase-0.5 spike** — wire the policy to match (passthrough branch if the spike found augmented-only forwarding).
- **424 → reroute is a POLICY, not built-in failover (RESOLVED in Phase 0).** Smoke proved built-in `failover` ignores HTTP status. Build a **response-phase routing policy**: on upstream status `== 424`, route to provider #2. Do NOT rely on `failover.enabled` for the escalation — it only catches connection/transport failures. Keep `failover` on as a separate safety net for a genuinely-down router.
- **Terminal fallback:** define what the client sees when BOTH providers fail (router 424 → big-model errors/timeout). Must be a clean error response, never a raw leaked 424. Test this path explicitly.
- Confirm cost tracking distinguishes local vs escalated (add `% escalated` metric).

**Deliverable:** exportable Gravitee API config; local-first + cloud-fallback works end-to-end; both-fail path returns clean error.

---

## Phase 4b — App-driven gateway config (UI + adapter)  *(gateway-agnostic; Gravitee = PoC impl)*

Instead of hand-clicking the gateway console, the app pushes the routing config via the gateway's **Management API**. Connection + auth come from a form in our UI (Phase 8 dashboard), so swapping gateways = swapping an adapter, not rewriting the app. PoC targets Gravitee APIM **v4** (LLM Proxy lives in v4 schema).

**UI collects (gateway-agnostic form):**
- MAPI base URL (e.g. `http://gravitee-mgmt:8083/management`)
- Auth: bearer token OR user/pass — pass-through to adapter, **never logged**.
- Org id + Env id (Gravitee MAPI paths are org/env scoped).
- Router endpoint URL + big-model provider creds (the two providers to register).

**Adapter interface (the abstraction):**
```
GatewayAdapter:
  test_connection(conn) -> ok/fail
  deploy_router(conn, router_url, fallback_provider) -> api_id
  undeploy(conn, api_id)
```
`ponytail:` one impl now (Gravitee). `if gateway_type == "gravitee"` is fine — define the interface shape, skip the plugin registry until a 2nd gateway exists.

**GraviteeAdapter deploy sequence (bare POST ≠ live route):**
1. `POST /apis/import` (or `/apis`) with API-definition JSON → `api_id`. **The JSON MUST embed the 424→reroute response-policy + both providers (router #1, big model #2)** — not just endpoint registration. This is the load-bearing piece; if the adapter registers only endpoints, the operator hand-wires the policy and the "zero console clicks" metric fails. `deploy_router(conn, router_url, fallback_provider)` owns the *whole* escalation mechanism, not just the router endpoint.
2. `POST /apis/{api_id}/plans` → KEY_LESS or API_KEY plan, publish it. (Plan body needs `definitionVersion:"V4"` + `security.configuration:{}` — Phase-0 gotcha.)
3. `POST /apis/{api_id}/deployments` (or `_deploy`) → push to gateway.
4. `POST /apis/{api_id}/_start`.

**Escalation-mechanism-per-adapter (post-Phase-0 note):** "swappable gateway" got heavier. Each adapter owns *how* its gateway reroutes on 424 (Gravitee = response-policy in the API def), not just endpoint registration. The `GatewayAdapter` interface is unchanged, but `deploy_router` for any future gateway must produce a working 424-reroute, not assume built-in failover.

**Verify before writing adapter:** exact MAPI paths + deploy verbs differ v3 vs v4 — pin **v4** (new `/apis` schema for AI/LLM proxy). Reuse an existing LLM-proxy API definition as the payload template (Phase 4).

**Check (runnable):** `test_connection` against live Gravitee returns ok; `deploy_router` produces a *started* API that routes; `undeploy` removes it. Assert MAPI token never appears in logs.

**Deliverable:** dashboard form → `GatewayAdapter` → Gravitee MAPI deploys a live, started routing API **whose definition already contains the 424→reroute response-policy + both providers**; a low-retrieval query escalates to the big model with **zero console clicks**; secrets pass-through only.

---

## Phase 5 — Packaging
- Single `docker-compose.yml`: `vectordb`, `router` (Ollama = host, not in compose — Phase 1).
- `.env.example`: model tag, `THRESHOLD`, collection name, big-model creds ref.
- `README`: one command + 60-sec "why" (local-first + Gravitee-fallback).
- `make demo`: ingests sample docs so first run isn't empty.

**Deliverable:** clone → `docker compose up` → working endpoint on a laptop, no GPU.

---

## Phase 6 — Eval + threshold tuning  *(replaces the "60-80% local" guess with a number)*
- **Fix the corpus first:** eval set is written *against the `./docs` ingested in Phase 2/5*. Freeze docs before labeling or labels drift.
- Build eval set: 20–30 queries labeled `stay-local` vs `escalate` (in-corpus questions → local; off-topic → escalate).
- Log per request: `top_score`, escalation outcome, correctness.
- Sweep `THRESHOLD`, pick value maximizing correct routing. Report real local-%.

**Deliverable:** defended `THRESHOLD` + re-runnable `eval.py` (rerun when models swap).

---

## Phase 7 — Dashboard  *(local quick-view + config knob)*
Small UI to watch savings and tweak the gate. **Served by the router itself** — no new container, no frontend framework. `ponytail:` Gravitee already owns real observability/cost dashboards; this is a lightweight local view, not a rebuild of them.

- **`GET /stats`** (JSON) on the router: cumulative counters kept in memory —
  - `total`, `answered_local`, `escalated`, `escalated_pct`
  - `cloud_calls_avoided` = `answered_local`; **`gross_cloud_cost_avoided_usd`** = `answered_local × cloud_price_per_call`. Cloud price from `.env` (`CLOUD_USD_PER_CALL`, rough). **Label it "gross avoided cost", NOT "savings"** — it ignores local compute and the escalated calls that still hit cloud. Someone will screenshot the tile.
  - `top_score` running histogram (buckets) — shows how the THRESHOLD line splits traffic.
  - current `THRESHOLD`, model tags, Qdrant collection size.
- **`GET /dashboard`** — one static HTML page (vanilla JS, polls `/stats` every ~2s). Stat tiles: local-%, gross-avoided-$ (labeled, not "savings"), req count; a bar/histogram of top_score with the THRESHOLD marked. Mirrors the headroom-style `:8787/dashboard` feel. Served at `:8081/dashboard`.
- **Config from UI (minimal):** THRESHOLD editable → `POST /config {threshold}`. Live-updates the in-memory gate; also writes back to `.env` so it survives restart. `ponytail:` only THRESHOLD is worth a knob in v1 — model tag / collection are restart-level, leave them read-only. **Guard:** `/config` + `/stats` reset are localhost-only (bind check), never exposed through the Gravitee route — config surface must not be publicly reachable.
- Counters are in-memory (`ponytail:` reset on restart — fine for a demo; add a `--persist` to a JSON file only if someone needs history across restarts).

**Check (runnable):** hit router with 1 local-hit + 1 escalate → `/stats` shows `total=2, escalated=1, escalated_pct=50`; `POST /config {threshold}` → subsequent `/stats` reflects new value.

**Deliverable:** `:8081/dashboard` renders live stats; THRESHOLD adjustable from UI and persisted.

---

## Phase 8 — Content (optional)
- Write up the 424 escalation-contract decision — genuinely novel, fits API-gateway-governance angle.
- Keep README scope narrow ("local-first RAG endpoint for Gravitee LLM Proxy") — avoid inviting RouteLLM / vLLM-Semantic-Router comparison.

---

## What changed from the two source plans
- **Kept** claude-plan's phase structure, 424 contract, eval phase.
- **Kept** qwen-plan's architecture diagram + component clarity.
- **Killed** the answerability gate (double model call) — v1 uses retrieval score only.
- **Killed** qwen's X-Escalate header — 424 status is a simpler signal than a custom header. (Phase-0 later showed 424 also needs a gateway response-policy; still simpler than a header + policy.)
- **Fixed** Phase 2: real ingestion + chunking, not fragments.
- **Fixed** the "60-80% local" claim — now an eval output, not an assumption.
- **Flagged** the unresolved risk: which prompt Gravitee forwards on 424 (Phase 0 + Phase 4).

## Gaps patched (2026-07-15)
- **Cold-start race** → host Ollama, router startup model-readiness ping + pre-pull (Phase 1).
- **Query extraction** → last user message only, defined (Phase 3).
- **Threshold undefined + unnormalized** → cosine metric + 768 dim + default 0.55 locked (Phase 0/2).
- **Empty collection** → `retrieve` returns `0.0`, escalates clean (Phase 2/3).
- **Both-providers-fail** → terminal fallback defined (Phase 4).
- **Streaming** → rejected with 400 in v1 (Phase 0/3).
- **PDF** → dropped from v1 (Phase 2). **Concurrency** → flagged, not solved (Phase 3). **Eval corpus order** → freeze docs first (Phase 6).
