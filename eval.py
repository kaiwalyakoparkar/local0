#!/usr/bin/env python3
"""Threshold tuning + retrieval quality report.

Reads a labeled eval set (queries tagged stay-local vs escalate), computes the
retrieval top_score and the retrieved chunks for each against the CURRENT Qdrant
collection, sweeps THRESHOLD, and reports:

  - the threshold that maximizes correct routing,
  - a confusion matrix at that threshold (false-local errors are the costly ones:
    they serve a wrong local answer instead of escalating),
  - local-class precision / recall,
  - retrieval MRR of the gold source (for stay-local rows that name a `source`).

Freeze the corpus and re-ingest BEFORE labeling — labels drift if it moves.
`make eval-fresh` re-ingests docs/sample and runs this for a reproducible number.

Eval set format (eval_set.json):
  [ {"query": "...", "label": "stay-local", "source": "sample/x.md"},
    {"query": "...", "label": "escalate"} ]

Usage:  python eval.py [eval_set.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app import rag

STEPS = [i / 100 for i in range(0, 101)]  # 0.00 .. 1.00


def _reciprocal_rank(chunks: list[dict], gold: str) -> float:
    for i, c in enumerate(chunks, start=1):
        if c.get("source") == gold:
            return 1.0 / i
    return 0.0


def main(path: str) -> None:
    rows = json.loads(Path(path).read_text())
    # One retrieval per query; keep score + chunks for MRR.
    graded = []
    for r in rows:
        chunks, score = rag.retrieve(r["query"])
        graded.append((r["label"], score, r.get("source"), chunks))

    def accuracy(t: float) -> float:
        return sum((s >= t) == (lbl == "stay-local")
                   for lbl, s, _, _ in graded) / len(graded)

    best_t = max(STEPS, key=accuracy)
    best_acc = accuracy(best_t)

    # Confusion at best_t (positive class = "answered local").
    tp = fp = fn = tn = 0
    for lbl, s, _, _ in graded:
        pred_local, actual_local = s >= best_t, lbl == "stay-local"
        if pred_local and actual_local:
            tp += 1
        elif pred_local and not actual_local:
            fp += 1          # false-local: served a wrong local answer (costly)
        elif not pred_local and actual_local:
            fn += 1          # needless escalation (cost = a cloud call)
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0

    gold_rows = [(g, ch) for lbl, _, g, ch in graded if lbl == "stay-local" and g]
    mrr = (sum(_reciprocal_rank(ch, g) for g, ch in gold_rows) / len(gold_rows)
           if gold_rows else float("nan"))

    print(f"queries          : {len(graded)}")
    print(f"best THRESHOLD   : {best_t:.2f}")
    print(f"routing accuracy : {best_acc * 100:.1f}%")
    print("confusion @ best :")
    print(f"  answered local, correct   (TP): {tp}")
    print(f"  answered local, WRONG     (FP): {fp}   <- costly false-local")
    print(f"  escalated, needless       (FN): {fn}")
    print(f"  escalated, correct        (TN): {tn}")
    print(f"local precision  : {precision * 100:.1f}%")
    print(f"local recall     : {recall * 100:.1f}%")
    print(f"retrieval MRR    : {mrr:.3f}  ({len(gold_rows)} rows with gold source)")
    print(f"\nSet THRESHOLD={best_t:.2f} in .env")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "eval_set.json")
