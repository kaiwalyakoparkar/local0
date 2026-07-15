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


def reset() -> None:
    global _total, _answered_local, _escalated, _hist
    with _lock:
        _total = _answered_local = _escalated = 0
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
            "histogram": list(_hist),
            "buckets": _BUCKETS,
        }
