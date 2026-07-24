# local0 Total Revamp — Production Grade + Architecture v2 + UI v2

## Context

local0 is a working PoC: FastAPI RAG router behind a Gravitee gateway, local Qwen3 0.6B via host Ollama, Qdrant vectors, HTTP 424 escalation contract (sacred — gateway response-policy reroutes on 424). Two audits found it functionally complete but not production-grade: outages 500 instead of escalating, blocking sync I/O in async handlers, spoofable Host-header control-plane auth, non-idempotent ingest, zero logging, no CI, no container hardening.

User direction: **total revamp** — production hardening **plus** architecture upgrade modeled on the 2026 local-RAG consensus (AnythingLLM / Open WebUI / RAGFlow class systems, production RAG guides) **plus** a full UI revamp.

Research consensus adopted: hybrid retrieval (dense + BM25-style sparse, RRF fusion) is the production baseline; reranking is highest-ROI but costly on CPU; chunking is fixed before anything else (300–500 tokens, 10–15% overlap, section-aware, metadata-enriched); citations in answers; retrieval-level eval (precision/recall/MRR) run in CI.

**User decisions:**
- Infra failures fail-open: 424 → cloud reroute (availability first).
- All phases, full scope.
- UI: full local-RAG UI, self-served, **no build step** (vanilla JS/CSS from FastAPI; keeps docker-compose-only ship).
- Retrieval: **hybrid dense+sparse with RRF**, cross-encoder reranker behind `RERANK` env flag, off by default.

**Design decisions (settled during planning):**
- **Blocking I/O → threadpool, not async rewrite.** Keep `ollama.py`/`rag.py` sync (ingest/eval import them sync); hot path via `fastapi.concurrency.run_in_threadpool`. Module-level shared `httpx.Client(transport=HTTPTransport(retries=1))` + cached `QdrantClient`.
- **Control-plane auth → `ADMIN_TOKEN` header** (`secrets.compare_digest`); unset → existing `_is_local()` fallback + warning (zero-config dev preserved).
- **Escalation gate signal stays dense cosine top-score** (threshold semantics, dashboard knob, and eval sweep all keep meaning); RRF fused ranking selects which chunks go into context. RRF rank scores are not comparable to a 0–1 threshold — don't gate on them.
- **Sparse vectors via `fastembed` Bm25** (qdrant-client extra) — no new service; Qdrant native sparse index + server-side RRF fusion (`prefetch` + `FusionQuery`).
- **Real token streaming impossible by design** — refusal gate must see full answer before 200-vs-424. One-delta SSE stays, documented.
- **Stats → JSON snapshot** (atomic `os.replace`), single-replica target; SQLite named as ceiling.
- **Skipped (ponytail, documented):** async client rewrite, Prometheus/OTel, router-side rate limiting (gateway's job), GraphRAG, multi-tenant workspaces, horizontal scale, node/React toolchain.

---

## Phase 1 — Hot-path correctness

Outages currently 500; must 424 (fail-open). Event loop blocks up to 120s per request.

**Files:** `app/main.py`, `app/ollama.py`, `app/rag.py`

1. `app/ollama.py`: module-level shared `httpx.Client` with connect-retries; guard `r.json()["embedding"]` (line 24) and `["message"]["content"]` (line 45) → raise `OllamaError` on missing keys / `httpx.HTTPError`.
2. `app/rag.py`: cache `QdrantClient` (line 19 builds per call); cache collection-exists check (lines 51-56) behind a flag, reset on error.
3. `app/main.py:132` `chat_completions`: guard `await req.json()` → 400; move blocking body into sync `_handle_chat(body)` called via `run_in_threadpool`; wrap `rag.retrieve` (155) / `ollama.chat` (161): on infra exception → log + `stats.record(0.0, escalated=True)` + **424**.
4. Helper `_json_or_none(req)` for unguarded `await req.json()` in `/config`, `/gateway/*`, `/demo/gateway-chat`.
5. Fold in: `_strip_think` also strips unterminated `<think>` prefix (current regex needs closing tag).

**Verify:** tests: mocked retrieve/chat raising → 424; malformed JSON → 400. Live: `docker compose stop vectordb` → POST `/v1/chat/completions` → 424 not 500.

## Phase 2 — Control-plane auth + input hardening

`_is_local()` (main.py:226) trusts client-settable Host header; guards deploy/config/secrets endpoints.

**Files:** `app/main.py`, `app/config.py`, `.env.example`

1. `config.py`: `ADMIN_TOKEN`, `LEARN_TOKEN`, `MAX_BODY_BYTES` (default 65536).
2. `_admin_ok(req)`: `secrets.compare_digest` vs `X-Admin-Token` when set, else `_is_local()` + one-time warning. Replace all `_is_local` gates.
3. `/learn` (line 445): enforce `LEARN_TOKEN` when set (gateway callout injects header); cap query/answer ~8000 chars. Closes cache-poisoning alongside tag gate.
4. Body-size middleware: `Content-Length > MAX_BODY_BYTES` → 413.
5. Stop echoing raw exception text to clients (main.py:344,376, demo) — generic out, `logging.exception` in.
6. `config.py:85-101`: atomic `.env` write (temp + `os.replace`).

Rate limiting: skip — gateway owns edge.

**Verify:** with `ADMIN_TOKEN=x`: no header → 403, header → 200; spoofed `Host: localhost` from another container → 403.

## Phase 3 — Observability + startup gate

**Files:** `app/main.py`, `app/ollama.py`, `app/rag.py`

1. Stdlib logging (`LOG_LEVEL` env); one structured line per chat request: request id (uuid4 hex, echoed as `X-Request-Id`), decision (local/escalate/infra-escalate), top_score, latency ms.
2. Lifespan startup gate: retry up to `STARTUP_TIMEOUT` (120s) for `ollama.models_ready()` + Qdrant reachable; exit non-zero on timeout (compose restart retries). Satisfies plan's "router does not start until models ready".
3. `/health` (line 178): include Qdrant reachability (reuse `rag.qdrant_status()`) in 503 condition.

**Verify:** start with Ollama stopped → waits, comes up after; `/health` with vectordb stopped → 503; per-request log line in `make logs`.

## Phase 4 — Retrieval architecture v2 (the core revamp)

Dense-only cosine → 2026-consensus hybrid pipeline. Also absorbs data-correctness fixes.

**Files:** `app/rag.py`, `ingest.py`, `app/main.py`, `app/config.py`, `requirements.txt`, `docker-compose.yml` (model cache volume)

1. **Collection v2** (`local0_v2`, `COLLECTION` env): named dense vector (nomic, 768, cosine) + sparse vector (`bm25`, Qdrant sparse index with IDF modifier). `ensure_collection` creates both + payload index on `ts` and `source`.
2. **Chunking v2** (`ingest.py`): section-aware recursive split — split on markdown headings, then paragraphs, target 300–500 tokens (word-approx), 10–15% overlap; payload metadata `{source, section, chunk_index, ts}`. Deterministic ids `uuid5(f"{source}#{chunk_index}")` (reuse `upsert_learned` pattern, rag.py:75); delete stale points per source before upsert → **idempotent re-ingest**.
3. **Hybrid query** (`rag.retrieve`): `query_points` with two `prefetch` branches (dense k=20, sparse Bm25 k=20) + `FusionQuery(RRF)` → top_k context chunks. Sparse text encoding via `fastembed` Bm25 (add `qdrant-client[fastembed]`; pin; cache model dir in a volume). **Gate score remains dense cosine top-score** from the dense prefetch — threshold/dashboard/eval semantics unchanged.
4. **Optional reranker:** `RERANK=on` env → fastembed cross-encoder (e.g. bge-reranker-base) reranks fused top-20 → top-k before prompt. Off by default; `ponytail:` comment names CPU latency ceiling.
5. **Citations:** `retrieve` returns chunks with `{source, section, score}`; non-stream response gains top-level `"sources": [...]` field (additive, OpenAI clients ignore unknown fields); SSE final chunk carries same. Prompt instructs model to answer from context only (already does).
6. **Document management API** (admin-gated, for UI): `POST /docs` (multipart or `{name, text}` — ingest one doc through chunker), `GET /docs` (distinct sources + chunk counts via scroll/facet), `DELETE /docs/{source}`. Reuses ingest chunker; no separate service.
7. `rag.list_learned`: `scroll(order_by=ts desc)` using new payload index (fixes newest-first beyond 100, rag.py:106-114).
8. `stats.py`: JSON snapshot load/save (lifespan + every 20th record), `STATS_PATH` under `./data` volume.
9. Migration: `make reingest` = drop v1 collection, ingest into v2. README note.

**Verify:** ingest twice → point count unchanged; hybrid query returns keyword-exact matches dense-only missed (add eval cases); gate score still 0–1; `RERANK=on` smoke; `/docs` CRUD roundtrip; restart preserves stats.

## Phase 5 — UI revamp (no build step)

Replace single dashboard.html with a small self-served multi-view app: vanilla JS + modern CSS, FastAPI static mount, zero npm.

**Files:** `app/ui/` (new: `index.html`, `app.js`, `style.css`), `app/main.py` (routes), delete-or-slim `app/dashboard.html`

Views (client-side tabs, one page):
1. **Chat playground:** talk to `/v1/chat/completions`; per-message routing badge (local answer vs 424→cloud— when 424, show "escalated" and optionally relay through `/demo/gateway-chat` to show the cloud answer); show `sources` citations (source + section) under local answers; latency + top_score per message.
2. **Documents:** list sources + chunk counts (`GET /docs`), upload/paste doc (`POST /docs`), delete (`DELETE /docs/{source}`); ingest progress feedback.
3. **Learned answers:** browse/search learned Q&A (`/learned`), newest-first.
4. **Routing & stats:** current dashboard content revamped — local vs escalated, savings, top_score histogram vs threshold, threshold slider + learn-tags editor (`/config`), health panel (`/health`, `/debug`).
5. Admin token: prompt once, localStorage, sent as `X-Admin-Token` on admin calls.

Design: load `dataviz`/`impeccable` skill guidance at implementation time for the stats view; dark/light aware; no external CDN (self-contained assets).

**Verify:** browser walkthrough — upload doc, ask question about it → local answer with citation; ask off-corpus question → escalated badge; threshold change persists; all views work with `ADMIN_TOKEN` set.

## Phase 6 — API contract

**Files:** `app/main.py`, `app/ollama.py`

1. Real `usage` from Ollama `prompt_eval_count`/`eval_count` (drop hardcoded zeros, main.py:105).
2. `_last_user_message` (line 79): accept OpenAI content-parts arrays (join text parts).
3. Multi-turn: pass prior non-system turns after the RAG context system message (today dropped, line 143); retrieval still keyed on last user message.
4. OpenAI error envelope `{"error":{message,type,code}}` on `/v1/*` 4xx. 424 body unchanged (gateway keys on status only — verified).
5. Streaming: one-delta SSE stays; comment refusal-gate constraint; `stream` default True stays (Hermes quirk, comment it).

**Verify:** tests for each; existing suite green.

## Phase 7 — Ops/delivery

**Files:** `docker-compose.yml`, `Dockerfile`, `Makefile`, `.dockerignore`, new `requirements-dev.txt`, new `pyproject.toml`, new `.github/workflows/ci.yml`

1. Compose: healthchecks both services (router → `/health`; qdrant TCP/readyz workaround — image lacks curl), `depends_on: condition: service_healthy`, `restart: unless-stopped`, `mem_limit` on router, `./data` volume, fastembed model-cache volume.
2. Dockerfile: non-root `USER`, `HEALTHCHECK`, pin base by digest.
3. Makefile: drop `sleep 6`/`sleep 5` → `docker compose up --build -d --wait`; add `reingest`.
4. `.dockerignore`: `.venv*`, `tests/`, `.git`.
5. `requirements-dev.txt` (`pytest`, `ruff`, `pip-audit`); minimal `pyproject.toml` (ruff + pytest config).
6. CI: ruff check → pytest → pip-audit (allow-fail initially).

**Verify:** fresh `make quickstart` no sleeps; kill qdrant → auto-restart; green Actions.

## Phase 8 — Eval v2, gateway robustness, docs

**Files:** `eval_set.json`, `eval.py`, new committed `docs/sample/`, `app/gateway.py`, new `tests/test_gateway.py`, `README.md`

1. Commit frozen corpus `docs/sample/` (un-gitignore subdir); grow eval set to ~25–30 incl. keyword-exact queries that prove hybrid value; `eval.py`: per-label precision/recall + confusion counts + retrieval MRR of gold source; threshold sweep unchanged. `make eval-fresh` = re-ingest sample + run (reproducible).
2. `gateway.py` deploy (121-140): on publish/start failure after import → best-effort `undeploy` + re-raise. Document api-key-in-definition limitation (Gravitee secret refs = upgrade path).
3. `tests/test_gateway.py`: pure-function tests on API-definition builder (URL validation, 424 flow condition present).
4. README: fix stale `pre-implementation` badge; document new UI, hybrid retrieval, `/docs` API; runbook (Ollama down → all-424 + cloud spend, Qdrant down, model missing, re-ingest/migration, threshold re-tune); single-process assumption (`--workers 1`).

**Verify:** `make eval-fresh` deterministic twice; gateway tests pass.

---

## Sequencing

1→2→3 core hardening first (small diffs, kill the worst risks). 4 (architecture) before 5 (UI needs `/docs` + citations). 6–8 independent after 4. Each phase ships alone, commit per phase. **First action: commit the current healthy working-tree changes** (keyword pre-gate, `_strip_think`, tests) before Phase 1.

Reuse: `upsert_learned` uuid5 pattern (rag.py:75), `rag.qdrant_status()` for health, existing `_is_local` as fallback, existing dashboard fetch patterns as UI starting point.
