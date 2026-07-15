"""Config loaded from environment / .env, plus a live-writable THRESHOLD.

THRESHOLD is the one knob the dashboard can change at runtime (Phase 8). Writing
it back to .env means a container restart keeps the tuned value. Everything else
is read-once at boot.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from dotenv import load_dotenv

# Repo root = parent of this file's package dir.
ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
GEN_MODEL = os.getenv("GEN_MODEL", "qwen3:0.6b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))

QDRANT_URL = os.getenv("QDRANT_URL", "http://vectordb:6333")
COLLECTION = os.getenv("COLLECTION", "docs")

CLOUD_USD_PER_CALL = float(os.getenv("CLOUD_USD_PER_CALL", "0.01"))
DOCS_DIR = os.getenv("DOCS_DIR", "./docs")

# Watched substrings for POST /learn. Empty = store nothing (safe default).
_LEARN_TAGS = [t.strip() for t in os.getenv("LEARN_TAGS", "refund,shipping").split(",") if t.strip()]

# --- THRESHOLD: read at boot, mutable at runtime, persisted to .env ---
_lock = threading.Lock()
_threshold = float(os.getenv("THRESHOLD", "0.55"))


def get_learn_tags() -> list[str]:
    return list(_LEARN_TAGS)


def tag_match(query: str) -> bool:
    """True if any LEARN_TAGS substring appears in query (case-insensitive)."""
    q = query.lower()
    return any(t.lower() in q for t in get_learn_tags())


def get_threshold() -> float:
    with _lock:
        return _threshold


def set_threshold(value: float) -> float:
    """Update the in-memory gate and persist to .env so it survives restart."""
    if not 0.0 <= value <= 1.0:
        raise ValueError("threshold must be in [0, 1] (cosine top_score range)")
    global _threshold
    with _lock:
        _threshold = value
        _persist_threshold(value)
        return _threshold


def _persist_threshold(value: float) -> None:
    """Rewrite the THRESHOLD line in .env in place (create the file if absent).

    ponytail: naive line rewrite, not a full dotenv writer. Fine for one key.
    """
    line = f"THRESHOLD={value}\n"
    if not ENV_PATH.exists():
        ENV_PATH.write_text(line)
        return
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.strip().startswith("THRESHOLD="):
            lines[i] = line
            break
    else:
        lines.append(line)
    ENV_PATH.write_text("".join(lines))
