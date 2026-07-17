"""In-memory routing counters + a top_score histogram for the dashboard.

ponytail: counters reset on restart — fine for a single-user demo. Add a
--persist JSON dump only if someone needs history across restarts.
"""
from __future__ import annotations

import threading

from . import config

_lock = threading.Lock()
_total = 0
_answered_local = 0
_escalated = 0
_learned = 0  # cloud answers cached back via /learn (tag matched, stored)
_learn_calls = 0  # every POST /learn received — 0 while escalating means the gateway never called back
# 20 buckets over cosine [0, 1] (negatives clamp to bucket 0).
_BUCKETS = 20
_hist = [0] * _BUCKETS


def _bucket(score: float) -> int:
    i = int(max(0.0, min(0.999, score)) * _BUCKETS)
    return min(i, _BUCKETS - 1)


def record(top_score: float, escalated: bool) -> None:
    global _total, _answered_local, _escalated
    with _lock:
        _total += 1
        if escalated:
            _escalated += 1
        else:
            _answered_local += 1
        _hist[_bucket(top_score)] += 1


def record_learned() -> None:
    global _learned
    with _lock:
        _learned += 1


def record_learn_call() -> None:
    global _learn_calls
    with _lock:
        _learn_calls += 1


def reset() -> None:
    global _total, _answered_local, _escalated, _learned, _learn_calls, _hist
    with _lock:
        _total = _answered_local = _escalated = _learned = _learn_calls = 0
        _hist = [0] * _BUCKETS


def snapshot() -> dict:
    with _lock:
        pct = (100.0 * _escalated / _total) if _total else 0.0
        return {
            "total": _total,
            "answered_local": _answered_local,
            "escalated": _escalated,
            "escalated_pct": round(pct, 1),
            # Gross cloud spend avoided (local answers × per-call price). Not net.
            "cloud_calls_avoided": _answered_local,
            "est_usd_avoided": round(_answered_local * config.CLOUD_USD_PER_CALL, 2),
            "threshold": config.get_threshold(),
            "learned": _learned,
            "learn_calls": _learn_calls,
            "learn_tags": config.get_learn_tags(),
            "histogram": list(_hist),
            "buckets": _BUCKETS,
        }
