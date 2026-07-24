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

# Control-plane auth. When ADMIN_TOKEN is set, mutating/secret endpoints require
# an X-Admin-Token header; unset falls back to the (spoofable) Host check for
# zero-config local dev. LEARN_TOKEN optionally gates /learn (gateway callout
# injects it). MAX_BODY_BYTES caps request size (unbounded prompts = DoS).
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
LEARN_TOKEN = os.getenv("LEARN_TOKEN", "")
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "65536"))
MAX_LEARN_CHARS = int(os.getenv("MAX_LEARN_CHARS", "8000"))

# Startup gate: how long to wait for Ollama models + Qdrant before giving up.
# On timeout the process exits non-zero so the container restart policy retries.
STARTUP_TIMEOUT = int(os.getenv("STARTUP_TIMEOUT", "120"))

# Persist routing counters across restarts. Empty = disabled (in-memory only).
# Compose sets this to a path on the ./data volume.
STATS_PATH = os.getenv("STATS_PATH", "")

# Hybrid-retrieval reranker: off by default (CPU cross-encoder adds latency).
RERANK = os.getenv("RERANK", "").lower() in ("1", "true", "on", "yes")
RERANK_MODEL = os.getenv("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
DOCS_DIR = os.getenv("DOCS_DIR", "./docs")
# Dashboard "Test via gateway" hits this from inside the container network.
GATEWAY_CHAT_URL = os.getenv(
    "GATEWAY_CHAT_URL",
    "http://apim-gateway:8082/local0/v1/chat/completions",
)

# Watched substrings for POST /learn. Empty = store nothing (safe default).
_LEARN_TAGS = [t.strip() for t in os.getenv("LEARN_TAGS", "gravitee").split(",") if t.strip()]

# --- THRESHOLD: read at boot, mutable at runtime, persisted to .env ---
_lock = threading.Lock()
_threshold = float(os.getenv("THRESHOLD", "0.55"))

# --- Gateway management connection: read at boot, rotatable at runtime, persisted.
# Without a live gateway the router's 424 escalation dead-ends, so the dashboard
# probes this connection to show whether escalations will route. Token rotates when
# the gateway container restarts; operator re-saves it via POST /gateway/connect.
_GATEWAY_KEYS = ("mapi_base", "org_id", "env_id", "token", "user", "password")
_gateway = {
    "mapi_base": os.getenv("GATEWAY_MAPI_BASE", ""),
    "org_id": os.getenv("GATEWAY_ORG_ID", ""),
    "env_id": os.getenv("GATEWAY_ENV_ID", ""),
    "token": os.getenv("GATEWAY_TOKEN", ""),
    "user": os.getenv("GATEWAY_USER", ""),
    "password": os.getenv("GATEWAY_PASSWORD", ""),
}


def get_learn_tags() -> list[str]:
    with _lock:
        return list(_LEARN_TAGS)


def set_learn_tags(tags) -> list[str]:
    """Replace the watched tags and persist to .env. Accepts a list or comma string."""
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, list):
        raise ValueError("tags must be a list or comma-separated string")
    clean = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
    global _LEARN_TAGS
    with _lock:
        _LEARN_TAGS = clean
        _set_env_key("LEARN_TAGS", ",".join(clean))
        return list(clean)


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
        _set_env_key("THRESHOLD", str(value))
        return _threshold


def get_gateway_conn() -> dict:
    """Current persisted gateway management connection (includes the bearer token)."""
    with _lock:
        return dict(_gateway)


def gateway_configured() -> bool:
    """True once enough is set to attempt a probe: mapi_base plus some auth."""
    with _lock:
        return bool(_gateway["mapi_base"] and (_gateway["token"] or _gateway["user"]))


def set_gateway_conn(fields: dict) -> None:
    """Persist the gateway connection (rotate token) and update the in-memory copy.

    mapi_base is required; any omitted key is cleared. Env keys are GATEWAY_<KEY>.
    """
    mapi = (fields.get("mapi_base") or "").strip()
    if not mapi:
        raise ValueError("mapi_base required")
    with _lock:
        for k in _GATEWAY_KEYS:
            v = (fields.get(k) or "").strip() if isinstance(fields.get(k), str) else ""
            _gateway[k] = mapi if k == "mapi_base" else v
            _set_env_key(f"GATEWAY_{k.upper()}", _gateway[k])


def _set_env_key(key: str, value: str) -> None:
    """Rewrite the `key=` line in .env in place (create the file if absent).

    ponytail: naive line rewrite, not a full dotenv writer. Fine for our few keys.
    """
    line = f"{key}={value}\n"
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines(keepends=True)
        for i, ln in enumerate(lines):
            if ln.strip().startswith(f"{key}="):
                lines[i] = line
                break
        else:
            lines.append(line)
        body = "".join(lines)
    else:
        body = line
    # Atomic: write a temp file then rename, so a crash mid-write can't truncate .env.
    tmp = ENV_PATH.with_suffix(ENV_PATH.suffix + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, ENV_PATH)
