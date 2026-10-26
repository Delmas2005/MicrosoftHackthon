"""A second, independent judge: Microsoft's groundedness evaluator (Azure AI Evaluation SDK, the
evaluators behind Microsoft Foundry's evaluations) on every card the screens show.

For each card, the evaluator gets the sentences on screen (the response) and the evidence each
sentence cites, with its true value (the context). It scores from 1 to 5 how well the response is
supported by the context; 3 or more is a pass. It runs on gpt-4.1-mini in our Foundry project.

As a control, it also gets the 5 trap cards of the Contradicteur demo: the TV card plus one sentence
with the right number and the wrong conclusion ("could see 0% of it, so the decision on the field
was a guess"). A judge worth having should mark them down.

    py -m pip install azure-ai-evaluation
    py -m evals.groundedness                     # about 2 minutes, results in evals/results.json
    py -m evals.groundedness --to-foundry        # also log the run to the Foundry project's
                                                 # Evaluations page (needs FOUNDRY_PROJECT_ENDPOINT
                                                 # in .env and `az login` first)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

from agents.card import MatchData
from agents.contradicteur import _evidence_for_review, plant_trap
from agents.laws_clerk import where

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "examples" / "cards_agents_match_001.jsonl"
OUT = ROOT / "evals" / "results.json"


def shown_cards(path: Path) -> list[dict]:
    """What the screens show: the highest revision of each (incident, phase, audience)."""
    best = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)
            k = (c["incident_id"], c["phase"], c["audience"])
            if k not in best or c.get("revision", 1) >= best[k].get("revision", 1):
                best[k] = c
    return list(best.values())


def row(card: dict, data: MatchData, kind: str) -> dict:
    inc = data.incident(card["incident_id"])
    off = inc["officiating"]
    context = {
        "incident": inc["headline"],
        "what the officials judge": inc["focal"]["what"],
        "where": where(inc),
        "decision on the field": off["on_field_decision"].replace("_", " "),
        "officials' final decision": (card["decision"]["value"].replace("_", " ")
                                      if card["phase"] == "decided" and card.get("decision") else "not made yet"),
        "evidence": [_evidence_for_review(e, data, card["incident_id"], card["phase"])
                     for c in card["claims"] for e in c["evidence"]],
    }
    who = {"tv": "TV viewers", "stadium": "the crowd in the stadium", "fan": "a supporter, on their phone",
           "audio": "a supporter who listens"}.get(card["audience"], card["audience"])
    return {
        "card": f"{card['incident_id']} {card['phase']} {card['audience']} rev {card.get('revision', 1)}",
        "kind": kind,
        "query": f"Explain the officials' handling of this incident to {who}: {inc['headline']}",
        "response": " ".join(c["text"] for c in card["claims"]),
        "context": json.dumps(context, ensure_ascii=False, default=str),
    }


def model_config() -> dict:
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    base = re.sub(r"/openai(/v1)?/?$", "", endpoint.rstrip("/"))  # the SDK wants the resource URL
    return {"azure_endpoint": base, "api_key": os.environ["AZURE_OPENAI_API_KEY"],
            "azure_deployment": os.environ.get("MODEL_FAST", "gpt-4.1-mini"), "api_version": "2024-10-21"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to-foundry", action="store_true", help="also log the run in the Foundry project")
    args = ap.parse_args()
    load_dotenv()
    from azure.ai.evaluation import GroundednessEvaluator

    data = MatchData(ROOT / "data" / "match_001")
    cards = shown_cards(CARDS)
    rows = [row(c, data, "shown") for c in cards]
    for c in cards:
        if c["phase"] == "decided" and c["audience"] == "tv":
            trapped = plant_trap(c, data)
            if trapped is not c:
                rows.append(row(trapped, data, "trap"))

    judge = GroundednessEvaluator(model_config())
    for i, r in enumerate(rows, 1):
        for wait in (0, 10, 30, 60):
            time.sleep(wait)
            try:
                res = judge(query=r["query"], response=r["response"], context=r["context"])
                break
            except Exception as exc:  # usually a rate limit: wait and try again
                res = {"groundedness": None, "groundedness_reason": f"{type(exc).__name__}: {exc}"}
        r["score"] = res.get("groundedness")
        r["passed"] = res.get("groundedness_result") == "pass"
        r["reason"] = res.get("groundedness_reason", "")
        print(f"  [{i}/{len(rows)}] {r['kind']:<5} {r['card']:<28} score {r['score']}  {r['reason'][:90]}")

    def summary(kind):
        rs = [r for r in rows if r["kind"] == kind and r["score"] is not None]
        if not rs:
            return {"cards": 0}
        return {"cards": len(rs), "mean_score": round(sum(r["score"] for r in rs) / len(rs), 2),
                "passed": sum(r["passed"] for r in rs), "full_marks": sum(r["score"] >= 5 for r in rs)}

    result = {"evaluator": "azure-ai-evaluation GroundednessEvaluator", "judge_model": model_config()["azure_deployment"],
              "shown": summary("shown"), "traps": summary("trap"), "rows": rows,
              "measured": time.strftime("%Y-%m-%d %H:%M")}
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    s, t = result["shown"], result["traps"]
    print(f"\nCards on screen: {s.get('passed')}/{s.get('cards')} pass, mean score {s.get('mean_score')} out of 5")
    print(f"Trap cards:      {t.get('passed')}/{t.get('cards')} pass (lower is better), mean score {t.get('mean_score')}")
    print(f"Saved to {OUT.relative_to(ROOT)}")

    if args.to_foundry:
        from azure.ai.evaluation import evaluate
        project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        if not project:
            raise SystemExit("Set FOUNDRY_PROJECT_ENDPOINT in .env (Foundry portal, project Overview page).")
        data_file = ROOT / "evals" / "rows.jsonl"
        data_file.write_text("".join(json.dumps({k: r[k] for k in ("card", "kind", "query", "response", "context")},
                                                ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        try:
            run = evaluate(data=str(data_file), evaluators={"groundedness": judge}, azure_ai_project=project,
                           evaluation_name="Fourth Official cards: groundedness",
                           evaluator_config={"groundedness": {"column_mapping": {
                               "query": "${data.query}", "response": "${data.response}", "context": "${data.context}"}}})
            print(f"Logged to Foundry: {run.get('studio_url', '(see the Evaluations page of the project)')}")
        except Exception as exc:
            print(f"Could not log to Foundry ({type(exc).__name__}: {exc}). The local results above still stand.")


if __name__ == "__main__":
    main()
