# local0 overview

local0 is a local-first RAG endpoint that sits behind an LLM gateway. It answers
cheap, in-scope questions with a small local model and escalates hard ones to a
big cloud model automatically.

The local model that answers questions is Qwen3 0.6B, served by Ollama on the
host machine. Embeddings use nomic-embed-text at 768 dimensions with cosine
similarity.

When document retrieval is strong, local0 answers locally and returns HTTP 200.
When retrieval is weak — the top similarity score falls below the routing
threshold — the router returns HTTP 424. A response-based routing policy on the
gateway rewrites that 424 into a reroute to the cloud provider.

local0 owns local model serving, the Qdrant vector database and retrieval, and
the escalation signal. The gateway owns routing, authentication, semantic cache,
guardrails, observability, and cost tracking.
