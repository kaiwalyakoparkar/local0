# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Implemented + production-hardened. Service code lives under `app/` (`main.py`, `rag.py`, `ollama.py`, `stats.py`, `gateway.py`, `config.py`) with a self-served UI in `app/ui/` (vanilla JS, no build step), `ingest.py`, `eval.py`, tests in `tests/`, `requirements-dev.txt` + `pyproject.toml` (pytest/ruff), CI in `.github/workflows/ci.yml`, and a `Makefile` (`make quickstart` / `demo` / `up` / `ingest` / `reingest` / `test` / `eval` / `eval-fresh`). Retrieval is **hybrid** (dense nomic + stdlib BM25 sparse, fused with Qdrant server-side RRF; gate score stays dense cosine). Control plane is auth-gated via `ADMIN_TOKEN`; infra failures fail **open** to 424. Build history follows `plans/merged-plan.md` and the revamp plan in `~/.claude/plans/`. Shipped via `docker compose` (router + qdrant; host Ollama), single-replica (`--workers 1`).

## What we're building

**local0** (Smart Local Router) — a local-first RAG endpoint that sits *behind an LLM gateway*. A small local model (Qwen3 0.6B via Ollama) answers when document retrieval is strong; when retrieval is weak the router returns **HTTP 424**, which a **response-based routing policy on the gateway** reroutes to a big cloud model.

- **This service owns:** local model serving, Qdrant vector DB + retrieval, the escalation signal.
- **The gateway owns:** routing, auth, semantic cache, guardrails, observability, cost tracking.
- The escalation contract is "router returns 424; a gateway response-policy reroutes on 424". Phase-0 smoke (2026-07-15, live APIM v4) **proved built-in failover ignores HTTP status** — it retries only on connection/transport failure, so the policy is mandatory, not optional. Read Phase 0 of the plan before touching the gate logic.
- **v1 adapter:** Gravitee APIM only (`GatewayAdapter` seam is ready for others; not built yet).

## Critical cross-repo context (not discoverable from this repo)

PoC plugs into the public Gravitee workshop stack
([gravitee-io-labs/Gravitee-AI-Agent-Workshop](https://github.com/gravitee-io-labs/Gravitee-AI-Agent-Workshop)),
typically checked out as a sibling at `../Gravitee-AI-Agent-Workshop`:

- Gravitee is **APIM**, gateway on **:8082**, docker network **`docker_default`** (compose project under that repo's `docker/`). `am-gateway :8092` is Access Management (auth) — not the LLM path.
- An **LLM Proxy already runs**: "Hermes LLM Proxy" at `/hermes-llm/` (ModelScope upstream), imported via that repo's `docker/setup.sh` / `docker/gravitee-management.yml`. Register `router-service` the same way — copy that template (`gravitee-init/apim-apis/Hermes-LLMs-1-0.json`).
- **Ollama already runs on the host** (:11434, native, not a container). See Phase 1 for reuse-host vs containerize decision.
- **Redis semantic cache already up** (`gio-workshop-redis :6379`).
- Gateway → router is **container DNS** (`http://router-service:8081`), not `localhost`. Router + Qdrant must join the external `docker_default` network to be reachable.

## Planned stack

**Host Ollama** (reuse, not containerized) serving **qwen3:0.6b** (already pulled) + **nomic-embed-text** (pull once), cosine, 768-dim · Qdrant · FastAPI router exposing OpenAI-compatible `/v1/chat/completions`, plus a self-served dashboard at `:8081/dashboard` (`/stats` JSON + THRESHOLD config knob; not exposed through the LLM gateway). Router reaches Ollama via `host.docker.internal:11434`. Ships as `docker compose up` (router + qdrant only). Commands: `make quickstart` (pull models → up → ingest), `make demo`, `make up`/`down`, `make ingest`, `make test`, `make eval`, `make logs` — see `Makefile` and README.
