"""Routing counters + a top_score histogram for the dashboard.

Counters live in-process (single-replica assumption — run uvicorn with
--workers 1) but are snapshotted to a JSON file so history survives a restart.
ponytail: JSON is enough for 7 counters + a 20-bucket histogram; reach for SQLite
only if this ever needs concurrent writers or per-request history.
"""
from __future__ import annotations

import json
import os
import threading

from . import config

_lock = threading.Lock()
_dirty_since_save = 0
_SAVE_EVERY = 20  # persist at most every N records to bound disk churn
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
    global _total, _answered_local, _escalated, _dirty_since_save
    with _lock:
        _total += 1
        if escalated:
            _escalated += 1
        else:
            _answered_local += 1
        _hist[_bucket(top_score)] += 1
        _dirty_since_save += 1
        due = _dirty_since_save >= _SAVE_EVERY
    if due:
        save()


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
    save()


def save() -> None:
    """Atomically snapshot the counters to STATS_PATH (temp file + rename)."""
    path = config.STATS_PATH
    if not path:
        return
    with _lock:
        global _dirty_since_save
        _dirty_since_save = 0
        data = {"total": _total, "answered_local": _answered_local,
                "escalated": _escalated, "learned": _learned,
                "learn_calls": _learn_calls, "hist": list(_hist)}
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # best-effort; losing a stats snapshot must never break a request


def load() -> None:
    """Restore counters from STATS_PATH on startup, if present."""
    path = config.STATS_PATH
    if not path or not os.path.exists(path):
        return
    global _total, _answered_local, _escalated, _learned, _learn_calls, _hist
    try:
        with open(path) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return
    with _lock:
        _total = int(d.get("total", 0))
        _answered_local = int(d.get("answered_local", 0))
        _escalated = int(d.get("escalated", 0))
        _learned = int(d.get("learned", 0))
        _learn_calls = int(d.get("learn_calls", 0))
        h = d.get("hist") or []
        if len(h) == _BUCKETS:
            _hist = [int(x) for x in h]


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
