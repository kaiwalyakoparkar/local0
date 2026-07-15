.PHONY: help setup models up down ingest demo quickstart eval test logs seed-learn

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

setup:   ## Create .env from the example (once)
	@test -f .env || cp .env.example .env && echo ".env ready"

models:  ## Pull both models into host Ollama (gen + embed)
	ollama pull qwen3:0.6b
	ollama pull nomic-embed-text

up:      ## Start router + qdrant
	docker compose up --build -d

down:    ## Stop everything
	docker compose down

ingest:  ## Ingest ./docs into Qdrant (run inside the router container)
	docker compose exec router-service python ingest.py

demo: setup up  ## One-shot: start, wait, ingest sample docs
	@echo "waiting for qdrant..." && sleep 5
	$(MAKE) ingest
	@echo "\nRouter ready → dashboard at http://localhost:8081/dashboard"

quickstart: models demo  ## Full setup from scratch: pull models → start → ingest → dashboard

eval:    ## Sweep THRESHOLD against eval_set.json
	docker compose exec router-service python eval.py

test:    ## Run unit tests (mocked — no Ollama/Qdrant needed)
	python -m pytest tests/ -q

logs:    ## Tail router logs
	docker compose logs -f router-service

# Fake gateway callback — same path Phase-4 policy will hit after cloud answer.
seed-learn: ## POST fake {query,answer} to /learn (proves tag-match → upsert)
	curl -sS -X POST http://localhost:8081/learn \
		-H 'Content-Type: application/json' \
		-d '{"query":"What is the refund policy?","answer":"Full refund within 30 days of purchase."}'
	@echo
