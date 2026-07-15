"""Qdrant retrieval + collection management.

retrieve() is the gate's only input: it returns the top-k chunks and the single
top cosine score the router thresholds on. An empty or absent collection returns
([], 0.0) so a fresh clone escalates cleanly instead of crashing.
"""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from . import config, ollama

TOP_K = 4


def client() -> QdrantClient:
    return QdrantClient(url=config.QDRANT_URL)


def ensure_collection(c: QdrantClient) -> None:
    """Create the collection at the pinned dim/metric, or assert an existing one matches.

    A dim mismatch means embeddings won't line up — hard error, never silently
    upsert into the wrong-shaped space.
    """
    if c.collection_exists(config.COLLECTION):
        info = c.get_collection(config.COLLECTION)
        dim = info.config.params.vectors.size
        if dim != config.EMBED_DIM:
            raise ValueError(
                f"collection '{config.COLLECTION}' has dim {dim}, expected "
                f"{config.EMBED_DIM}. Drop it or fix EMBED_DIM — cannot mix dims."
            )
        return
    c.create_collection(
        collection_name=config.COLLECTION,
        vectors_config=qm.VectorParams(size=config.EMBED_DIM, distance=qm.Distance.COSINE),
    )


def retrieve(query: str, k: int = TOP_K) -> tuple[list[dict], float]:
    """Embed `query`, search Qdrant, return (chunks, top_score).

    top_score is the highest cosine similarity in [-1, 1]. Empty/absent
    collection → ([], 0.0).
    """
    c = client()
    if not c.collection_exists(config.COLLECTION):
        return [], 0.0
    vec = ollama.embed(query)
    hits = c.query_points(
        collection_name=config.COLLECTION, query=vec, limit=k, with_payload=True
    ).points
    if not hits:
        return [], 0.0
    chunks = [{"text": h.payload.get("text", ""), "source": h.payload.get("source", "")}
              for h in hits]
    return chunks, float(hits[0].score)


def upsert_learned(query: str, answer: str) -> None:
    """Embed Q+A from a gateway /learn callback and upsert into the collection."""
    text = f"Q: {query}\nA: {answer}"
    c = client()
    ensure_collection(c)
    c.upsert(
        collection_name=config.COLLECTION,
        points=[qm.PointStruct(
            id=str(uuid.uuid4()),
            vector=ollama.embed(text),
            payload={"text": text, "source": "learn", "query": query},
        )],
    )
