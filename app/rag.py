"""Qdrant retrieval + collection management.

retrieve() is the gate's only input: it returns the top-k chunks and the single
top cosine score the router thresholds on. An empty or absent collection returns
([], 0.0) so a fresh clone escalates cleanly instead of crashing.

Retrieval v2 is hybrid: a dense (nomic embedding) branch and a sparse BM25 branch
fused server-side with Reciprocal Rank Fusion. The BM25 sparse vector is built in
stdlib (tokenize → crc32 term ids → term frequencies) and Qdrant applies IDF via
the sparse index's IDF modifier — no fastembed/onnx dependency, deterministic
across the ingest and router processes.

The escalation GATE score stays the dense cosine top-score: RRF produces rank
scores that aren't comparable to the 0–1 threshold, so hybrid only changes which
chunks feed the prompt, never the routing decision.
"""
from __future__ import annotations

import re
import uuid
import zlib

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from . import config, ollama

TOP_K = 4
PREFETCH_K = 20         # candidates per branch before fusion (retrieve wide, fuse narrow)
DENSE = "dense"         # named dense vector
SPARSE = "bm25"         # named sparse vector

# Shared client (pooled) + a one-shot "collection exists" flag so the hot path
# skips a collection_exists() round-trip on every request after the first hit.
_client: QdrantClient | None = None
_collection_seen = False

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=config.QDRANT_URL)
    return _client


def bm25_sparse(text: str) -> qm.SparseVector:
    """Build a BM25 term-frequency sparse vector.

    Tokens are lowercased word chars, hashed to stable uint32 ids via crc32 (must
    match across ingest + query processes — Python's salted hash() would not).
    Values are raw term frequencies; Qdrant's IDF modifier does the weighting.
    """
    counts: dict[int, int] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) < 2:
            continue
        tid = zlib.crc32(tok.encode()) & 0x7FFFFFFF
        counts[tid] = counts.get(tid, 0) + 1
    return qm.SparseVector(indices=list(counts.keys()), values=[float(v) for v in counts.values()])


def _named_vectors(text: str) -> dict:
    """Both vectors for one point: dense embedding + sparse BM25."""
    return {DENSE: ollama.embed(text), SPARSE: bm25_sparse(text)}


def ensure_collection(c: QdrantClient) -> None:
    """Create the hybrid collection, or assert an existing one is compatible.

    A dim mismatch or an old dense-only (unnamed-vector) collection is a hard
    error — never silently upsert into the wrong-shaped space. The message points
    at `make reingest`, which drops and rebuilds under the v2 schema.
    """
    if c.collection_exists(config.COLLECTION):
        info = c.get_collection(config.COLLECTION)
        vectors = info.config.params.vectors
        if not isinstance(vectors, dict) or DENSE not in vectors:
            raise ValueError(
                f"collection '{config.COLLECTION}' predates hybrid retrieval "
                f"(no named '{DENSE}' vector). Run `make reingest` to rebuild it."
            )
        dim = vectors[DENSE].size
        if dim != config.EMBED_DIM:
            raise ValueError(
                f"collection '{config.COLLECTION}' has dim {dim}, expected "
                f"{config.EMBED_DIM}. Drop it or fix EMBED_DIM — cannot mix dims."
            )
        return
    c.create_collection(
        collection_name=config.COLLECTION,
        vectors_config={DENSE: qm.VectorParams(size=config.EMBED_DIM, distance=qm.Distance.COSINE)},
        sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
    )
    # Index ts/source so learned entries sort newest-first server-side and doc
    # deletion can filter by source without a full scan.
    c.create_payload_index(config.COLLECTION, "ts", qm.PayloadSchemaType.FLOAT)
    c.create_payload_index(config.COLLECTION, "source", qm.PayloadSchemaType.KEYWORD)


def retrieve(query: str, k: int = TOP_K) -> tuple[list[dict], float]:
    """Hybrid search; return (chunks, top_score).

    top_score is the dense cosine similarity of the best dense hit — the gate's
    only input, semantics unchanged from v1. Chunks are the RRF-fused top-k across
    the dense + sparse branches. Empty/absent collection → ([], 0.0).
    """
    global _collection_seen
    c = client()
    if not _collection_seen:
        if not c.collection_exists(config.COLLECTION):
            return [], 0.0
        _collection_seen = True  # exists once → skip the round-trip on later calls
    try:
        dense_vec = ollama.embed(query)
        # Gate score: pure dense cosine (unaffected by fusion).
        dense_hits = c.query_points(
            collection_name=config.COLLECTION, query=dense_vec, using=DENSE,
            limit=max(k, 1), with_payload=True,
        ).points
        top_score = float(dense_hits[0].score) if dense_hits else 0.0
        # Context chunks: RRF fusion of dense + sparse branches.
        fused = c.query_points(
            collection_name=config.COLLECTION,
            prefetch=[
                qm.Prefetch(query=dense_vec, using=DENSE, limit=PREFETCH_K),
                qm.Prefetch(query=bm25_sparse(query), using=SPARSE, limit=PREFETCH_K),
            ],
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            limit=k, with_payload=True,
        ).points
    except Exception:
        _collection_seen = False  # collection may have been dropped; re-check next time
        raise
    hits = fused or dense_hits
    if not hits:
        return [], 0.0
    if config.RERANK:
        hits = _rerank(query, hits, k)
    chunks = [{"text": h.payload.get("text", ""),
               "source": h.payload.get("source", ""),
               "section": h.payload.get("section", "")}
              for h in hits]
    return chunks, top_score


def _rerank(query: str, hits: list, k: int) -> list:
    """Cross-encoder rerank of fused candidates. Off unless RERANK=on.

    ponytail: lazy-imports fastembed only when enabled, so the base install stays
    dependency-light. CPU cross-encoder adds latency on every request — the ceiling
    is throughput; leave off unless eval shows fusion alone misranks.
    """
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        global _reranker
        if _reranker is None:
            _reranker = TextCrossEncoder(config.RERANK_MODEL)
        docs = [h.payload.get("text", "") for h in hits]
        scored = sorted(zip(_reranker.rerank(query, docs), hits, strict=False),
                        key=lambda t: t[0], reverse=True)
        return [h for _, h in scored[:k]]
    except Exception:  # fastembed missing / model download failed → keep fused order
        return hits[:k]


_reranker = None


def upsert_learned(query: str, answer: str) -> None:
    """Embed Q+A from a gateway /learn callback and upsert into the collection."""
    import time
    text = f"Q: {query}\nA: {answer}"
    c = client()
    ensure_collection(c)
    c.upsert(
        collection_name=config.COLLECTION,
        points=[qm.PointStruct(
            # Deterministic id keyed on the (normalized) query so re-learning the
            # same question overwrites instead of piling up duplicate points.
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, query.strip().lower())),
            vector=_named_vectors(text),
            payload={"text": text, "source": "learn", "query": query,
                     "answer": answer, "ts": time.time()},
        )],
    )


def qdrant_status() -> dict:
    """Qdrant reachability + point counts for the dashboard diagnostics.

    Answers "is the vector DB alive and does it hold anything" from the router —
    the only process that can reach Qdrant (its port isn't host-published).
    """
    try:
        c = client()
        if not c.collection_exists(config.COLLECTION):
            return {"reachable": True, "collection": False, "total": 0, "learned": 0}
        total = c.count(config.COLLECTION).count
        learned = c.count(config.COLLECTION, count_filter=qm.Filter(must=[
            qm.FieldCondition(key="source", match=qm.MatchValue(value="learn"))])).count
        return {"reachable": True, "collection": True, "total": total, "learned": learned}
    except Exception:
        return {"reachable": False, "collection": False, "total": 0, "learned": 0}


def list_learned(limit: int = 100) -> list[dict]:
    """Cached (learned) Q/A entries, newest first — powers the dashboard cache view.

    order_by on the indexed `ts` field makes "newest first" correct at any size;
    the old Python-side sort only saw the first `limit` scroll rows, so beyond 100
    entries the genuinely-newest ones could be missed entirely.
    """
    c = client()
    if not c.collection_exists(config.COLLECTION):
        return []
    points, _ = c.scroll(
        collection_name=config.COLLECTION, limit=limit, with_payload=True,
        order_by=qm.OrderBy(key="ts", direction=qm.Direction.DESC),
        scroll_filter=qm.Filter(must=[
            qm.FieldCondition(key="source", match=qm.MatchValue(value="learn"))]),
    )
    return [{"query": p.payload.get("query", ""),
             "answer": p.payload.get("answer", ""),
             "ts": p.payload.get("ts", 0)} for p in points]


def list_sources() -> list[dict]:
    """Distinct ingested doc sources with chunk counts — powers the Documents view."""
    c = client()
    if not c.collection_exists(config.COLLECTION):
        return []
    counts: dict[str, int] = {}
    offset = None
    while True:
        points, offset = c.scroll(
            collection_name=config.COLLECTION, limit=256, offset=offset,
            with_payload=["source"],
        )
        for p in points:
            src = p.payload.get("source", "")
            if src and src != "learn":
                counts[src] = counts.get(src, 0) + 1
        if offset is None:
            break
    return sorted(({"source": s, "chunks": n} for s, n in counts.items()),
                  key=lambda r: r["source"])


def delete_source(source: str) -> None:
    """Remove every chunk of one ingested doc (for the Documents delete action)."""
    c = client()
    if not c.collection_exists(config.COLLECTION):
        return
    c.delete(config.COLLECTION, points_selector=qm.Filter(must=[
        qm.FieldCondition(key="source", match=qm.MatchValue(value=source))]))


def add_document(source: str, text: str) -> int:
    """Chunk + embed one document under `source`, replacing any prior version.

    Returns the number of chunks stored. Deletes existing points for the same
    source first, so re-adding is idempotent (mirrors ingest.py).
    """
    from ingest import chunk_sections  # local import avoids a cycle at module load
    c = client()
    ensure_collection(c)
    delete_source(source)
    ts = __import__("time").time()
    points = []
    for i, (section, piece) in enumerate(chunk_sections(text)):
        points.append(qm.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}#{i}")),
            vector=_named_vectors(piece),
            payload={"text": piece, "source": source, "section": section,
                     "chunk_index": i, "ts": ts},
        ))
    if points:
        c.upsert(collection_name=config.COLLECTION, points=points)
    return len(points)
