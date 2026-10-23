"""Reference systems for RefBench.

    python -m refbench.baselines

on_field      : trusts the live call every time and never asks VAR to intervene.
                This is the bar any assistant has to beat.
rules_oracle  : applies refbench/rules.py to the facts and measures visibility with the
                Referee's Eye tools. Scores 100% by construction: it proves the answer key is
                internally consistent, it is not a competitor.
"""
from __future__ import annotations

import json
from pathlib import Path

from refbench.adapter import analyze_item
from refbench.evaluate import DATA, score
from refbench.rules import decide

OUT = Path(__file__).parent / "predictions"


def main():
    OUT.mkdir(exist_ok=True)
    items = [json.loads(l) for l in (DATA / "items.jsonl").read_text().splitlines()]
    on_field, oracle = [], []
    for it in items:
        on_field.append({"item_id": it["item_id"], "decision": it["on_field_decision"], "should_var_intervene": False})
        dec, border, _ = decide(it["family"], it["facts"])
        vis = analyze_item(it)["observers"]["REF"]["mean_visible_share_in_critical_window"]
        intervene = it["on_field_decision"] != dec and (it["family"] == "offside" or not border)
        oracle.append({"item_id": it["item_id"], "decision": dec, "should_var_intervene": intervene,
                       "referee_visible_share": vis})
    for name, rows in (("on_field", on_field), ("rules_oracle", oracle)):
        p = OUT / f"{name}.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        print(json.dumps(score(p), indent=2))


if __name__ == "__main__":
    main()
