# Retrieval and routing

local0 stores document chunks in Qdrant, the vector database. Retrieval is
hybrid: a dense embedding branch and a sparse BM25 keyword branch are fused with
Reciprocal Rank Fusion so that both semantic meaning and exact keyword matches
contribute to the results.

The escalation decision uses the dense cosine top score. If that score is at or
above the configured routing threshold, local0 answers locally; otherwise it
escalates. The threshold is tunable at runtime from the dashboard and persisted
to the environment file.

Documents are ingested with section-aware chunking: text is split on markdown
headings, then packed into overlapping windows of roughly 380 words. Re-ingesting
the same document is idempotent — deterministic ids plus a per-source delete mean
a re-run overwrites instead of duplicating the corpus.

Answers cite their sources: each local answer lists the document and section that
grounded it.
