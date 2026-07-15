<div align="center">

# 🧭 local0

**Local-first RAG endpoint that answers cheap questions locally and escalates hard ones to the cloud — automatically, behind your Gravitee LLM Proxy.**

![status](https://img.shields.io/badge/status-pre--implementation-orange?style=for-the-badge)
![router](https://img.shields.io/badge/FastAPI-router-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![vectordb](https://img.shields.io/badge/Qdrant-vector%20DB-DC244C?style=for-the-badge)
![llm](https://img.shields.io/badge/Ollama-qwen3%3A0.6b-000000?style=for-the-badge&logo=ollama&logoColor=white)
![gateway](https://img.shields.io/badge/Gravitee-APIM-6E42FF?style=for-the-badge)
![gpu](https://img.shields.io/badge/GPU-not%20required-success?style=for-the-badge)

[Quickstart](#-quickstart) · [Dashboard](#dashboard-8081dashboard) · [Config](#configuration) · [Gateway wiring](#gateway-wiring-gravitee)

</div>

---

A **local-first RAG endpoint** that sits *behind* a Gravitee LLM Proxy. A small
local model (Qwen3 0.6B via Ollama) answers when document retrieval is strong.
When retrieval is weak the router returns **HTTP 424**, and a response-based
routing policy on the gateway reroutes the request to a big cloud model.

**Why:** cheap, private, local answers for in-corpus questions; automatic
escalation to the cloud only when the local corpus can't help. No GPU.

```
Agent → Gravitee LLM Proxy → router-service (200 local)  or  424 → cloud model
                                   │
                          Qdrant + host Ollama (Qwen3 0.6B + nomic-embed-text)
```

## Prereqs

- Docker + Docker Compose
- **Ollama installed + running on the host** (`:11434`). `make quickstart` pulls the models for you.

## 🚀 Quickstart

One command — checks prereqs, clones, pulls models, starts, ingests:

```bash
curl -fsSL https://raw.githubusercontent.com/<owner>/local0/main/install.sh | bash
```

Then open **http://localhost:8081/dashboard**, tune `THRESHOLD`, and you're live.

> Piping to `bash` runs remote code — [read `install.sh`](install.sh) first if you'd rather. It just clones the repo and runs `make quickstart` = `make models` (pull `qwen3:0.6b` + `nomic-embed-text`) → `make demo` (setup `.env` → start router+qdrant → ingest `./docs`).

Prefer to clone yourself?

```bash
git clone https://github.com/<owner>/local0.git && cd local0 && make quickstart
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
histogram with the threshold marker, and a **THRESHOLD slider** that persists to
`.env`. `/config` and `/stats/reset` are **localhost-only** — never exposed
through the gateway.

## Configuration

Everything lives in `.env` (see `.env.example`). Key knobs:

| Var | Meaning |
|---|---|
| `THRESHOLD` | escalate (424) when retrieval top_score < this. Default `0.55` (pre-eval guess; tune with `make eval`). |
| `OLLAMA_URL` | host Ollama. `host.docker.internal:11434` from a container. |
| `GEN_MODEL` / `EMBED_MODEL` | `qwen3:0.6b` / `nomic-embed-text` (768-dim, cosine). |
| `COLLECTION` | Qdrant collection name. |
| `CLOUD_USD_PER_CALL` | dashboard cost estimate (gross avoided, not net). |

## Gateway wiring (Gravitee)

The router only *raises* the 424 flag; **the gateway routes**. Built-in Gravitee
failover ignores HTTP status (proven by the Phase-0 smoke), so escalation needs a
**response-based routing policy**: on upstream `424`, reroute to provider #2.

`app/gateway.py` (`GraviteeAdapter`) pushes an API definition that embeds **both
providers + the 424→reroute policy** via the Management API — see
`plans/merged-plan.md` Phase 4/4b. Concrete policy plugin ids should be copied
from an existing LLM-proxy API definition in your APIM stack.

> **Open item (Phase 0.5):** confirm the gateway forwards the *original* user
> messages (not the RAG-augmented body) to the cloud model on reroute; add a
> passthrough branch if not.

## Layout

```
app/main.py       FastAPI: /v1/chat/completions, /stats, /config, /dashboard, /health
app/rag.py        Qdrant retrieve() + collection management
app/ollama.py     host Ollama embed + chat
app/stats.py      in-memory routing counters + histogram
app/gateway.py    GatewayAdapter + GraviteeAdapter (config push)
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
