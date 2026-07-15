# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Implemented. Service code lives under `app/` (`main.py`, `rag.py`, `ollama.py`, `stats.py`, `gateway.py`, `config.py`) with `ingest.py`, `eval.py`, mocked tests in `tests/`, and a `Makefile` (`make quickstart` / `demo` / `up` / `ingest` / `test` / `eval`). Build follows `plans/merged-plan.md` (the authoritative plan). Not yet published/deployed; Python service shipped via `docker compose`, not a package.

## What we're building

**local0** (Smart Local Router) — a local-first RAG endpoint that sits *behind the Gravitee LLM Proxy*. A small local model (Qwen3 0.6B via Ollama) answers when document retrieval is strong; when retrieval is weak the router returns **HTTP 424**, which a **response-based routing policy on the gateway** reroutes to a big cloud model.

- **This service owns:** local model serving, Qdrant vector DB + retrieval, the escalation signal.
- **Gravitee owns:** routing, auth, semantic cache, guardrails, observability, cost tracking.
- The escalation contract is "router returns 424; a gateway response-policy reroutes on 424". Phase-0 smoke (2026-07-15, live APIM v4) **proved built-in failover ignores HTTP status** — it retries only on connection/transport failure, so the policy is mandatory, not optional. Read Phase 0 of the plan before touching the gate logic.

## Critical cross-repo context (not discoverable from this repo)

This router plugs into a **separate Gravitee APIM Docker stack** (not shipped here):

- Gravitee is **APIM v4**; gateway typically on **:8082**. Attach router + Qdrant to that stack's Docker network (compose often names it `docker_default` — confirm locally).
- Register `router-service` like any other LLM-proxy upstream: import an API definition pointing at `http://router-service:8081`.
- **Ollama already runs on the host** (:11434, native, not a container). See Phase 1 for reuse-host vs containerize decision.
- Reuse the stack's Redis for Gravitee semantic cache if present — nothing to add in this repo.
- Gateway → router is **container DNS** (`http://router-service:8081`), not `localhost`.

## Planned stack

**Host Ollama** (reuse, not containerized) serving **qwen3:0.6b** (already pulled) + **nomic-embed-text** (pull once), cosine, 768-dim · Qdrant · FastAPI router exposing OpenAI-compatible `/v1/chat/completions`, plus a self-served dashboard at `:8081/dashboard` (`/stats` JSON + THRESHOLD config knob; localhost-only, not exposed through Gravitee). Router reaches Ollama via `host.docker.internal:11434`. Ships as `docker compose up` (router + qdrant only). Commands: `make quickstart` (pull models → up → ingest), `make demo`, `make up`/`down`, `make ingest`, `make test`, `make eval`, `make logs` — see `Makefile` and README.
