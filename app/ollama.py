"""Thin Ollama client: embeddings + chat completion against the host process.

Uses Ollama's native /api routes (not the OpenAI-compat shim) — one dependency
fewer and the shapes are simpler for our two calls.
"""
from __future__ import annotations

import httpx

from . import config

# CPU Qwen serializes and can be slow on first token; generous read timeout.
_TIMEOUT = httpx.Timeout(120.0, connect=5.0)


def embed(text: str) -> list[float]:
    """Return the embedding vector for `text` via nomic-embed-text."""
    r = httpx.post(
        f"{config.OLLAMA_URL}/api/embeddings",
        json={"model": config.EMBED_MODEL, "prompt": text},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    vec = r.json()["embedding"]
    if len(vec) != config.EMBED_DIM:
        raise ValueError(
            f"embedding dim {len(vec)} != configured EMBED_DIM {config.EMBED_DIM}; "
            f"fix EMBED_DIM or EMBED_MODEL"
        )
    return vec


def chat(messages: list[dict]) -> str:
    """Non-streaming chat completion via qwen3:0.6b. Returns the answer text."""
    r = httpx.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={"model": config.GEN_MODEL, "messages": messages, "stream": False},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


def models_ready() -> tuple[bool, list[str]]:
    """Check both required models are pulled. Used by /health readiness gate.

    Returns (ok, missing). Ollama tags carry a `:latest` suffix when the pull
    used a bare name, so match on prefix.
    """
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=httpx.Timeout(5.0))
        r.raise_for_status()
    except httpx.HTTPError:
        return False, [config.GEN_MODEL, config.EMBED_MODEL]
    have = {m["name"] for m in r.json().get("models", [])}

    def present(name: str) -> bool:
        base = name.split(":")[0]
        return any(h == name or h.split(":")[0] == base for h in have)

    missing = [m for m in (config.GEN_MODEL, config.EMBED_MODEL) if not present(m)]
    return (not missing), missing
