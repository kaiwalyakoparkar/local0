<div align="center">

<img src="assets/logo.svg" alt="local0" width="360" />

**Local-first RAG endpoint that answers cheap questions locally and escalates hard ones to the cloud — automatically, behind your LLM gateway.**

![status](https://img.shields.io/badge/status-pre--implementation-orange?style=for-the-badge)
![router](https://img.shields.io/badge/FastAPI-router-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![vectordb](https://img.shields.io/badge/Qdrant-vector%20DB-DC244C?style=for-the-badge)
![llm](https://img.shields.io/badge/Ollama-qwen3%3A0.6b-000000?style=for-the-badge&logo=ollama&logoColor=white)
![gateway](https://img.shields.io/badge/LLM%20gateway-agnostic-555555?style=for-the-badge)
![gpu](https://img.shields.io/badge/GPU-not%20required-success?style=for-the-badge)

[Quickstart](#-quickstart) · [Dashboard](#dashboard-8081dashboard) · [Config](#configuration) · [Gateway wiring](#gateway-wiring)

</div>

---

A **local-first RAG endpoint** that sits *behind* an LLM gateway as one provider.
A small local model (Qwen3 0.6B via Ollama) answers when document retrieval is
strong. When retrieval is weak the router returns **HTTP 424**, and a
response-based routing policy on the gateway reroutes the request to a big
cloud model.

**Why:** cheap, private, local answers for in-corpus questions; automatic
escalation to the cloud only when the local corpus can't help. No GPU.

```
Agent → LLM gateway → router-service (200 local)  or  424 → cloud model
                            │
                   Qdrant + host Ollama (Qwen3 0.6B + nomic-embed-text)
```

The router is **gateway-agnostic** (it only emits the 424 signal). Config push
goes through a `GatewayAdapter` so swapping vendors is an adapter swap, not a
rewrite. **v1 ships one adapter** (Gravitee APIM) — see Gateway wiring below.

## Prereqs

- Docker + Docker Compose
- **Ollama installed + running on the host** (`:11434`). `make quickstart` pulls the models for you.

## 🚀 Quickstart

One command — checks prereqs, clones, pulls models, starts, ingests:

```bash
curl -fsSL https://raw.githubusercontent.com/kaiwalyakoparkar/local0/main/install.sh | bash
```

Then open **http://localhost:8081/dashboard**, tune `THRESHOLD`, and you're live.

> Piping to `bash` runs remote code — [read `install.sh`](install.sh) first if you'd rather. It just clones the repo and runs `make quickstart` = `make models` (pull `qwen3:0.6b` + `nomic-embed-text`) → `make demo` (setup `.env` → start router+qdrant → ingest `./docs`).

Prefer to clone yourself?

```bash
git clone https://github.com/kaiwalyakoparkar/local0.git && cd local0 && make quickstart
```

Already have the models pulled? Run `make demo` instead of `quickstart`.

### Try it in 30 seconds

Send an OpenAI-compatible request:

```bash
curl localhost:8081/v1/chat/completions -H 'content-type: application/json' -d '{
  "messages": [{"role": "user", "content": "what does the router return on weak retrieval?"}]
}'
```

- In-corpus query → `200` with a local answer.
- Off-topic query → `424 {"detail": "no local context, escalate"}` (the escalation signal).

### Manual steps

```bash
make setup     # cp .env.example .env
make up        # docker compose up --build -d  (router + qdrant)
make ingest    # ingest ./docs into Qdrant
make test      # unit tests (mocked; needs `pip install pytest`)
make eval      # sweep THRESHOLD against eval_set.json (Phase 6)
make down
```

## Dashboard (`:8081/dashboard`)

Router-served, single page, no framework. Live routing counters, a top-score
histogram with the threshold marker, a **THRESHOLD slider**, and a **Learn tags**
field — comma-separated keywords worth caching (persists to `.env` as
`LEARN_TAGS`). `/config` and `/stats/reset` are **local-network only** (loopback
plus private/Docker bridge IPs) — never exposed through the gateway. Save actions
show an error message if the request is rejected.

### Learn loop (gateway callback)

When retrieval misses and the gateway reroutes to the cloud, its response-policy
posts the final answer back to `POST /learn {query, answer}`. If the query
contains one of the `LEARN_TAGS`, the router vectorizes the Q&A into Qdrant so the
same question answers locally next time. `/learn` is reachable from the gateway
(not localhost-only); the dashboard's **Test /learn** button fakes the callback
locally. ponytail: no auth on `/learn` in v1 — add a shared secret if it's ever
exposed beyond the gateway network.

## Configuration

Everything lives in `.env` (see `.env.example`). Key knobs:

| Var | Meaning |
|---|---|
| `THRESHOLD` | escalate (424) when retrieval top_score < this. Default `0.55` (pre-eval guess; tune with `make eval`). |
| `OLLAMA_URL` | host Ollama. `host.docker.internal:11434` from a container. |
| `GEN_MODEL` / `EMBED_MODEL` | `qwen3:0.6b` / `nomic-embed-text` (768-dim, cosine). |
| `COLLECTION` | Qdrant collection name. |
| `CLOUD_USD_PER_CALL` | dashboard cost estimate (gross avoided, not net). |
| `LEARN_TAGS` | substrings that must appear in a query before `POST /learn` stores it. |

## Gateway wiring

The router only *raises* the 424 flag; **the gateway routes**. Many gateways'
built-in failover ignores HTTP status (retries only on transport failure), so
escalation needs a **response-based routing policy**: on upstream `424`,
reroute to provider #2.

`app/gateway.py` defines `GatewayAdapter` and pushes an API definition that
embeds **both providers + the 424→reroute policy** via the gateway Management
API — see `plans/merged-plan.md` Phase 4/4b.

> **Current PoC:** only a Gravitee APIM adapter is implemented. Wire it against
> your APIM stack (sibling
> [Gravitee-AI-Agent-Workshop](https://github.com/gravitee-io-labs/Gravitee-AI-Agent-Workshop)
> works as a reference; copy the Hermes LLM Proxy API definition shape). A
> second vendor adapter is a non-goal for v1.

> **Open item (Phase 0.5):** confirm the gateway forwards the *original* user
> messages (not the RAG-augmented body) to the cloud model on reroute; add a
> passthrough branch if not.

## Layout

```
app/main.py       FastAPI: /v1/chat/completions, /stats, /config, /dashboard, /learn, /health
app/rag.py        Qdrant retrieve() + collection management
app/ollama.py     host Ollama embed + chat
app/stats.py      in-memory routing counters + histogram
app/gateway.py    GatewayAdapter (+ Gravitee PoC impl)
app/dashboard.html
ingest.py         walk ./docs → chunk → embed → upsert
eval.py           threshold sweep over a labeled eval set
tests/            mocked unit tests (no live services)
plans/            PRD.md + merged-plan.md (authoritative plan)
docs/             knowledge-base corpus ingested into Qdrant
```

## Non-goals (v1)

Streaming (`stream:true` → 400), multi-turn rewriting (last user message only),
PDF ingestion, concurrency (CPU Qwen serializes — single-user demo), a second
gateway adapter.
