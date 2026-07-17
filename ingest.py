#!/usr/bin/env python3
"""Ingest ./docs (md/txt) into Qdrant: walk, chunk, embed, upsert.

ponytail: fixed ~500-token / ~50-overlap word splitter. Upgrade to semantic
chunking only if Phase 6 retrieval eval is poor. PDF dropped in v1 — add a pypdf
branch only when a demo doc needs it.

Usage:  python ingest.py [docs_dir]
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

from qdrant_client.http import models as qm

from app import config, ollama, rag

CHUNK_WORDS = 380       # ~500 tokens at ~0.75 words/token
OVERLAP_WORDS = 40      # ~50 tokens
EXTS = {".md", ".txt"}


def chunk(text: str) -> list[str]:
    words = text.split()
    if not words:
        return []
    step = CHUNK_WORDS - OVERLAP_WORDS
    out = []
    for start in range(0, len(words), step):
        piece = words[start:start + CHUNK_WORDS]
        out.append(" ".join(piece))
        if start + CHUNK_WORDS >= len(words):
            break
    return out


def main(docs_dir: str) -> None:
    root = Path(docs_dir)
    files = [p for p in root.rglob("*") if p.suffix.lower() in EXTS]
    if not files:
        print(f"No .md/.txt files under {root}, skipping ingest", file=sys.stderr)
        return

    c = rag.client()
    rag.ensure_collection(c)

    points, total_chunks = [], 0
    for f in files:
        text = f.read_text(errors="ignore")
        for piece in chunk(text):
            vec = ollama.embed(piece)
            points.append(qm.PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={"text": piece, "source": str(f.relative_to(root))},
            ))
            total_chunks += 1
        # Flush per file to keep memory flat on large corpora.
        if points:
            c.upsert(collection_name=config.COLLECTION, points=points)
            points = []
        print(f"ingested {f.relative_to(root)}")

    print(f"done: {len(files)} files, {total_chunks} chunks -> "
          f"collection '{config.COLLECTION}'")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else config.DOCS_DIR)
