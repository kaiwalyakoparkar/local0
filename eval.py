#!/usr/bin/env python3
"""Phase 6 — threshold tuning. Replaces the "60-80% local" guess with a number.

Reads a labeled eval set (queries tagged stay-local vs escalate), computes the
retrieval top_score for each against the CURRENT Qdrant collection, sweeps
THRESHOLD, and reports the value that maximizes correct routing plus the real
local-answer %.

Freeze ./docs and re-ingest BEFORE labeling — labels drift if the corpus moves.
Rerun whenever the embedding model changes.

Eval set format (eval_set.json):
  [ {"query": "...", "label": "stay-local"}, {"query": "...", "label": "escalate"} ]

Usage:  python eval.py [eval_set.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app import rag

STEPS = [i / 100 for i in range(0, 101)]  # 0.00 .. 1.00


def correct(top_score: float, threshold: float, label: str) -> bool:
    routed_local = top_score >= threshold
    return routed_local == (label == "stay-local")


def main(path: str) -> None:
    rows = json.loads(Path(path).read_text())
    scored = [(r["label"], rag.retrieve(r["query"])[1]) for r in rows]

    best_t, best_acc = 0.0, -1.0
    for t in STEPS:
        acc = sum(correct(s, t, lbl) for lbl, s in scored) / len(scored)
        if acc > best_acc:
            best_acc, best_t = acc, t

    local_pct = 100.0 * sum(s >= best_t for _, s in scored) / len(scored)
    print(f"queries        : {len(scored)}")
    print(f"best THRESHOLD : {best_t:.2f}")
    print(f"routing acc    : {best_acc * 100:.1f}%")
    print(f"local-answer % : {local_pct:.1f}%")
    print(f"\nSet THRESHOLD={best_t:.2f} in .env")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "eval_set.json")
