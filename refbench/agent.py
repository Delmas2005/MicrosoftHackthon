"""The Fourth Official agents on RefBench.

For each incident, a VAR-assistant agent (gpt-5-mini on Foundry) gets what a VAR room would have:
- the facts established on review (how late the contact was, where the arm was...),
- the live call made on the field,
- the Referee's Eye measurements (what the referee could see, where he could have seen it better),
- the clauses of the Laws of the Game that apply, retrieved from the Laws index in Azure AI Search
  (the same index the Laws Clerk uses), plus the VAR protocol.
It never sees the answer key (refbench/rules.py, refbench/data/answers.jsonl).

It answers: the correct decision under the Laws, and whether VAR should step in (only for a
clear and obvious error, or for a factual matter such as offside). The predictions are scored by
refbench/evaluate.py, against the bar of trusting the on-field call.

Run (from the repo root):
    py -m refbench.agent --per-family 20       # 100 incidents, about 10 to 15 minutes
    py -m refbench.evaluate refbench/predictions/agents.jsonl refbench/predictions/on_field.jsonl

Interrupted? Run the same command again: incidents already answered are kept and skipped.
"""
from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agents.llm import MODEL_STRONG, client, parse_json
from refbench.adapter import analyze_item
from refbench.evaluate import DATA, score

OUT = Path(__file__).parent / "predictions" / "agents.jsonl"

DECISIONS = {
    "penalty_contact": ["penalty", "no_penalty"],
    "handball": ["penalty", "no_penalty"],
    "holding": ["penalty", "no_penalty"],
    "offside": ["offside", "no_offside"],
    "dogso": ["red_card", "yellow_card"],
}
FAMILY_TEXT = {
    "penalty_contact": "a challenge in the penalty area: penalty or not?",
    "handball": "the ball touching a defender's arm in the penalty area: penalty or not?",
    "holding": "holding in the penalty area: penalty or not?",
    "offside": "a goal-scoring move: was the attacker offside?",
    "dogso": "a foul on an attacker breaking towards goal: red card (denying an obvious goal-scoring "
             "opportunity) or yellow card?",
}
LAW_QUERIES = {  # what to look up in the Laws for each kind of incident
    "penalty_contact": [("careless reckless excessive force tackles challenges", "Law 12")],
    "handball": [("handling the ball deliberately touches hand arm unnaturally bigger", "Law 12")],
    "holding": [("holds an opponent impedes contact", "Law 12")],
    "offside": [("offside position involved in active play interfering", "Law 11")],
    "dogso": [("denying a goal or an obvious goal-scoring opportunity", "Law 12")],
}
VAR_QUERY = ("clear and obvious error serious missed incident review", None)

SYSTEM_PROMPT = """You are the VAR assistant of the Fourth Official system. For one refereeing
incident you get: the facts established on review, the decision the referee made live on the field,
measurements of what the referee could see, and the relevant pages of the IFAB Laws of the Game
2025/26 and the VAR protocol.

1. Decide the correct decision under the Laws, from the facts. Apply the Laws as written; when a
   fact sits close to a line (a light contact, a short hold), say it is borderline.
2. Decide whether VAR should intervene. VAR only intervenes when the decision on the field is a
   clear and obvious error, or for a factual matter such as an offside position, which is
   measured, not judged. A borderline judgement is the referee's call: VAR does not re-referee it.
   If your decision is the same as the decision on the field, VAR does not intervene.
3. What the referee could see explains why a call was missed. It does not change what the Laws say.

Answer with this JSON object and nothing else:
{"decision": one of the allowed decisions, "borderline": true or false,
 "should_var_intervene": true or false, "reason": "one sentence citing the Law and the facts"}"""


# --------------------------------------------------------------------------- what the agent reads

_law_cache: dict[str, str] = {}
_law_lock = threading.Lock()


def law_pages(family: str) -> str:
    """The pages of the Laws for this kind of incident, and the VAR protocol (looked up once)."""
    from agents.laws_clerk import search_laws

    with _law_lock:
        if family not in _law_cache:
            pages: dict[int, dict] = {}
            for query, law in LAW_QUERIES[family] + [VAR_QUERY]:
                for p in search_laws(query, law, top=2):
                    pages.setdefault(p["page"], p)
            _law_cache[family] = "\n\n".join(
                f"[{p['section']}, PDF page {p['page']}]\n{p['text']}" for p in sorted(pages.values(), key=lambda p: p["page"]))
        return _law_cache[family]


def describe(item: dict) -> str:
    eye = analyze_item(item)
    ref = eye["observers"]["REF"]
    measures = {
        "referee_distance_m": ref.get("distance_to_focal_m"),
        "share_of_the_incident_the_referee_could_see": ref.get("mean_visible_share_in_critical_window"),
        "best_view_within_reach_would_show": eye.get("best_view", {}).get("best", {}).get("visible_share"),
    }
    return json.dumps({
        "incident": FAMILY_TEXT[item["family"]],
        "what_the_officials_needed_to_see": item["focal"]["what"],
        "facts_established_on_review": item["facts"],
        "decision_on_the_field": item["on_field_decision"],
        "allowed_decisions": DECISIONS[item["family"]],
        "referees_eye": measures,
    }, indent=1, default=str)


# --------------------------------------------------------------------------- the agent

def judge(item: dict) -> dict:
    """One incident: the agent's prediction in RefBench format."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": describe(item) + "\n\nLaws of the Game:\n" + law_pages(item["family"])}]
    allowed = DECISIONS[item["family"]]
    answer: dict = {}
    for attempt in range(2):
        reply = client().chat.completions.create(model=MODEL_STRONG, messages=messages, max_completion_tokens=6000)
        content = reply.choices[0].message.content
        try:
            answer = parse_json(content)
        except (ValueError, TypeError):
            answer = {}
        if answer.get("decision") in allowed:
            break
        messages += [{"role": "assistant", "content": content or ""},
                     {"role": "user", "content": f"'decision' must be one of {allowed}. Answer with the JSON object again."}]
    decision = answer.get("decision") if answer.get("decision") in allowed else item["on_field_decision"]
    intervene = bool(answer.get("should_var_intervene")) and decision != item["on_field_decision"]
    if item["family"] == "offside":  # factual: a different answer from the measured line is always a VAR matter
        intervene = decision != item["on_field_decision"]
    return {"item_id": item["item_id"], "decision": decision, "should_var_intervene": intervene,
            "borderline": bool(answer.get("borderline")), "reason": answer.get("reason", ""),
            "answered": answer.get("decision") in allowed}


def pick(items: list[dict], per_family: int) -> list[dict]:
    """The first `per_family` incidents of each kind: the same subset every run."""
    out, count = [], {}
    for it in items:
        if count.get(it["family"], 0) < per_family:
            out.append(it)
            count[it["family"]] = count.get(it["family"], 0) + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-family", type=int, default=20, help="incidents per kind (5 kinds); 100 = all 500")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    items = [json.loads(l) for l in (DATA / "items.jsonl").read_text().splitlines()]
    todo = pick(items, args.per_family)
    out = Path(args.out)
    done = {}
    if out.exists():
        done = {r["item_id"]: r for r in (json.loads(l) for l in out.read_text().splitlines() if l.strip())}
    todo = [it for it in todo if it["item_id"] not in done]
    print(f"{len(done)} incidents already answered, {len(todo)} to go")

    lock = threading.Lock()
    with out.open("a", encoding="utf-8") as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(judge, it): it for it in todo}
        for i, job in enumerate(as_completed(jobs), 1):
            it = jobs[job]
            try:
                r = job.result()
            except Exception as exc:  # leave it out: the next run will try it again
                print(f"  {it['item_id']}: failed ({type(exc).__name__}: {exc}), will retry on the next run")
                continue
            with lock:
                f.write(json.dumps(r) + "\n")
                f.flush()
            print(f"  [{i}/{len(todo)}] {it['item_id']} {it['family']:<16} on field: {it['on_field_decision']:<12}"
                  f" agents: {r['decision']:<12} VAR: {'yes' if r['should_var_intervene'] else 'no'}")

    # Score on exactly the incidents the agents answered, next to the on-field call on the same ones.
    answered = {json.loads(l)["item_id"] for l in out.read_text().splitlines() if l.strip()}
    on_field = out.parent / "on_field_same_items.jsonl"
    on_field.write_text("".join(json.dumps({"item_id": it["item_id"], "decision": it["on_field_decision"],
                                            "should_var_intervene": False}) + "\n"
                                for it in items if it["item_id"] in answered))
    scores = {}
    for path in (on_field, out):
        s = score(path)
        scores[path] = s
        print(f"\n{s['system']}: {s['items_scored']} incidents")
        for k in ("decision_accuracy", "var_precision", "var_recall", "overreach_rate", "final_accuracy"):
            print(f"  {k:<18} {s[k]}")
        print(f"  by family          {s['decision_accuracy_by_family']}")

    # The figures for the README and the video (video/scenes.html reads this file).
    ag, of = scores[out], scores[on_field]
    pct = lambda v: "n/a" if v is None else f"{round(v * 100)}%"
    summary = {
        "items": ag["items_scored"],
        "on_field_accuracy": of["decision_accuracy"],
        "agents_accuracy": ag["decision_accuracy"],
        "facts": [["of clear errors flagged for VAR", pct(ag["var_recall"])],
                  ["of VAR calls justified", pct(ag["var_precision"])],
                  ["final decisions right, with VAR", pct(ag["final_accuracy"])]],
        "agents": ag, "on_field": of,
    }
    video = Path(__file__).resolve().parents[1] / "video" / "refbench.json"
    video.parent.mkdir(exist_ok=True)
    video.write_text(json.dumps(summary, indent=1))
    print(f"\nSummary written to {video.relative_to(video.parents[1])}")


if __name__ == "__main__":
    main()
