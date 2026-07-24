.PHONY: help setup models up down fresh ingest reingest demo quickstart eval eval-fresh test logs seed-learn

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

setup:   ## Create .env from the example (once)
	@test -f .env || cp .env.example .env && echo ".env ready"

models:  ## Pull both models into host Ollama (gen + embed)
	ollama pull qwen3:0.6b
	ollama pull nomic-embed-text

up:      ## Start router + qdrant (waits for healthchecks)
	docker compose up --build -d --wait

down:    ## Stop everything
	docker compose down

fresh:   ## Clean slate: wipe Qdrant volume, rebuild, restart, re-ingest (use to test updates)
	docker compose down -v
	docker compose up --build -d --wait
	$(MAKE) ingest
	@echo "\nFresh stack up → dashboard at http://localhost:8081/dashboard (cache empty, tags=gravitee)"

ingest:  ## Ingest ./docs into Qdrant (run inside the router container)
	docker compose exec router-service python ingest.py

reingest:  ## Drop the collection and re-ingest under the current schema (v2 hybrid)
	docker compose exec router-service python -c "from app import rag, config; c=rag.client(); c.delete_collection(config.COLLECTION) if c.collection_exists(config.COLLECTION) else None; print('dropped', config.COLLECTION)"
	docker compose exec router-service python ingest.py

demo: setup up  ## One-shot: start (waits for healthchecks), ingest sample docs
	$(MAKE) ingest
	@echo "\nRouter ready → dashboard at http://localhost:8081/dashboard"

quickstart: models demo  ## Full setup from scratch: pull models → start → ingest → dashboard

eval:    ## Sweep THRESHOLD against eval_set.json
	docker compose exec router-service python eval.py

eval-fresh: ## Reproducible eval: re-ingest the committed sample corpus, then sweep
	docker compose exec router-service python ingest.py docs/sample
	docker compose exec router-service python eval.py

test:    ## Run unit tests (mocked — no Ollama/Qdrant needed)
	python -m pytest tests/ -q

logs:    ## Tail router logs
	docker compose logs -f router-service

# Fake gateway callback — same path Phase-4 policy will hit after cloud answer.
seed-learn: ## POST fake {query,answer} to /learn (proves tag-match → upsert)
	curl -sS -X POST http://localhost:8081/learn \
		-H 'Content-Type: application/json' \
		-d '{"query":"What is Gravitee?","answer":"Gravitee is an open-source API management platform (APIM)."}'
	@echo
