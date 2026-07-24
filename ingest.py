#!/usr/bin/env python3
"""Ingest ./docs (md/txt) into Qdrant: walk, section-chunk, embed, upsert.

Chunking is section-aware: split on markdown headings first, then pack paragraphs
into ~300–500-token windows with ~12% overlap (the 2026 RAG default). Each chunk
carries its section heading as metadata for citations. Re-ingesting is idempotent:
deterministic ids (uuid5 of source#index) plus a per-source delete before upsert,
so a re-run overwrites instead of duplicating the corpus.

Usage:  python ingest.py [docs_dir]
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

from qdrant_client.http import models as qm

from app import config, rag

CHUNK_WORDS = 380       # ~500 tokens at ~0.75 words/token
OVERLAP_WORDS = 45      # ~12% overlap
EXTS = {".md", ".txt"}
_HEADING = "\n#"        # markdown heading marker at a line start


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split into (heading, body) sections on markdown headings; no headings → one."""
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    buf: list[str] = []
    for ln in lines:
        if ln.lstrip().startswith("#"):
            if buf:
                sections.append((heading, buf))
            heading = ln.lstrip("# ").strip()
            buf = []
        else:
            buf.append(ln)
    if buf or not sections:
        sections.append((heading, buf))
    return [(h, "\n".join(b).strip()) for h, b in sections if "\n".join(b).strip() or h]


def _window(words: list[str]) -> list[str]:
    """Pack words into overlapping ~CHUNK_WORDS windows."""
    if not words:
        return []
    step = CHUNK_WORDS - OVERLAP_WORDS
    out = []
    for start in range(0, len(words), step):
        out.append(" ".join(words[start:start + CHUNK_WORDS]))
        if start + CHUNK_WORDS >= len(words):
            break
    return out


def chunk_sections(text: str) -> list[tuple[str, str]]:
    """Return [(section_heading, chunk_text)] for one document."""
    out: list[tuple[str, str]] = []
    for heading, body in _split_sections(text):
        combined = f"{heading}\n{body}" if heading else body
        for piece in _window(combined.split()):
            out.append((heading, piece))
    return out


# Back-compat: the old chunk() returned plain strings; keep it for the unit test.
def chunk(text: str) -> list[str]:
    return [piece for _, piece in chunk_sections(text)]


def main(docs_dir: str) -> None:
    root = Path(docs_dir)
    files = [p for p in root.rglob("*") if p.suffix.lower() in EXTS]
    if not files:
        print(f"No .md/.txt files under {root}, skipping ingest", file=sys.stderr)
        return

    c = rag.client()
    rag.ensure_collection(c)

    total_chunks = 0
    for f in files:
        source = str(f.relative_to(root))
        text = f.read_text(errors="ignore")
        # Idempotent: drop this file's prior points before re-adding.
        rag.delete_source(source)
        ts = time.time()
        points = []
        for i, (section, piece) in enumerate(chunk_sections(text)):
            points.append(qm.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}#{i}")),
                vector=rag._named_vectors(piece),
                payload={"text": piece, "source": source, "section": section,
                         "chunk_index": i, "ts": ts},
            ))
        if points:
            c.upsert(collection_name=config.COLLECTION, points=points)
            total_chunks += len(points)
        print(f"ingested {source} ({len(points)} chunks)")

    print(f"done: {len(files)} files, {total_chunks} chunks -> "
          f"collection '{config.COLLECTION}'")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else config.DOCS_DIR)
