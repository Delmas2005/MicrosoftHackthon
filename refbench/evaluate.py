"""Score a system on RefBench.

    python -m refbench.evaluate refbench/predictions/on_field.jsonl

Prediction format, one JSON object per line:
    {"item_id": "RB-0001", "decision": "no_penalty", "should_var_intervene": false,
     "referee_visible_share": 0.42}          # last field optional

Reported:
  decision_accuracy        did the system reach the correct decision under the Laws?
  var_precision / recall   when it says "VAR should step in", is it right, and does it catch the clear errors?
  overreach_rate           how often it would re-referee a borderline call (VAR is not meant to)
  final_accuracy           decision that ends up standing if officials followed the system
  visibility_mae           error on how much of the incident the referee could see (if predicted)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data"


def load(path):
    return {r["item_id"]: r for r in (json.loads(l) for l in Path(path).read_text().splitlines() if l.strip())}


def score(pred_path, data_dir=DATA) -> dict:
    items = load(Path(data_dir) / "items.jsonl")
    answers = load(Path(data_dir) / "answers.jsonl")
    preds = load(pred_path)
    fam = defaultdict(lambda: {"n": 0, "correct": 0})
    tp = fp = fn = over = border_err = vis_n = 0
    vis_err = 0.0
    final_ok = correct = n = 0
    for iid, a in answers.items():
        p = preds.get(iid)
        if not p:
            continue
        n += 1
        f = a["family"]
        fam[f]["n"] += 1
        ok = p["decision"] == a["correct_decision"]
        correct += ok
        fam[f]["correct"] += ok
        pi, ti = bool(p["should_var_intervene"]), a["should_var_intervene"]
        tp += pi and ti
        fp += pi and not ti
        fn += ti and not pi
        on_field = items[iid]["on_field_decision"]
        if a["borderline"] and on_field != a["correct_decision"]:
            border_err += 1
            over += pi
        final = p["decision"] if pi else on_field
        final_ok += final == a["final_decision_after_var"]
        if "referee_visible_share" in p and p["referee_visible_share"] is not None:
            vis_n += 1
            vis_err += abs(p["referee_visible_share"] - a["referee_visible_share"])
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return {
        "system": Path(pred_path).stem,
        "items_scored": n,
        "items_total": len(answers),
        "decision_accuracy": round(correct / n, 3) if n else None,
        "decision_accuracy_by_family": {k: round(v["correct"] / v["n"], 3) for k, v in sorted(fam.items())},
        "var_precision": round(prec, 3) if prec is not None else None,
        "var_recall": round(rec, 3) if rec is not None else None,
        "overreach_rate": round(over / border_err, 3) if border_err else None,
        "final_accuracy": round(final_ok / n, 3) if n else None,
        "visibility_mae": round(vis_err / vis_n, 3) if vis_n else None,
    }


if __name__ == "__main__":
    for path in sys.argv[1:]:
        print(json.dumps(score(path), indent=2))
