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
Start again from zero: add --fresh.

Held-out check (incidents never used while we wrote the prompt):
    py -m refbench.agent --per-family 20 --split heldout
The prompt was tuned on the first 20 incidents of each kind ("tuning"). "heldout" takes the next 20
of each kind, which we never looked at; it is the honest score.

What we learnt from the first run (60.5% of decisions right, against 82.6% for the on-field call):
the facts were handed over as raw fields ("margin_m": -0.57), and the agent read a negative margin
as offside; and it applied "holds an opponent" or "contact" literally, as if any contact were a foul.
A VAR room does not work from raw fields or from the bare text of the Laws: it sees the picture and
applies refereeing guidance. So the facts are now written out in words (facts_in_words), and the
agent gets one page of refereeing guidance (GUIDANCE) on top of the Laws. The answer key's exact
thresholds (how late, how long, how far) are not in that guidance.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
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

# Refereeing guidance: how the Laws are applied in practice, in the words a referee is taught.
GUIDANCE = """Refereeing guidance (how the Laws are applied in practice):
- Contact on its own is not a foul. A challenge is a foul when it is careless, reckless or uses
  excessive force. A defender who clearly plays the ball first has not committed a foul unless the
  follow-through that comes after the ball is reckless or uses excessive force.
- Holding is penalised when it stops an opponent moving freely. When both players are holding each
  other, or the hold does not impede the opponent, play usually goes on.
- Handball: a natural position or movement of the arm for that action is not an offence; an arm
  that makes the body unnaturally bigger is. A ball that deflects off the player's own body onto
  the arm is usually not an offence unless the arm made the body unnaturally bigger deliberately.
- Offside position is factual: only the measurement at the moment of the pass decides it. A player
  level with or behind the second-last opponent is not in an offside position.
- Denying an obvious goal-scoring opportunity needs all of: a short distance to goal, the general
  direction of play towards goal, a likely chance of keeping or gaining control of the ball, and no
  defender able to cover. Without one of them, it is stopping a promising attack: a yellow card."""

SYSTEM_PROMPT = """You are the VAR assistant of the Fourth Official system. For one refereeing
incident you get: the facts established on review, the decision the referee made live on the field,
measurements of what the referee could see, refereeing guidance, and the relevant pages of the IFAB
Laws of the Game 2025/26 and the VAR protocol.

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


def facts_in_words(item: dict) -> list[str]:
    """The facts established on review, written out the way a VAR room would see them."""
    f, fam = item["facts"], item["family"]
    if fam == "penalty_contact":
        out = [f"The defender played the ball first; the contact came {f['contact_after_ball_s']:.2f} s after the ball."
               if f["ball_played_first"] else "The defender did not play the ball."]
        out.append(f"Force of the contact: {f['contact_force']}. Contact on the attacker's {f['contact_point'].replace('_', ' ')}.")
        return out
    if fam == "offside":
        cm = round(abs(f["margin_m"]) * 100)
        out = [f"At the moment of the pass, the attacker was {cm} cm beyond the second-last defender, nearer to the goal line."
               if f["margin_m"] > 0 else
               f"At the moment of the pass, the attacker was {cm} cm behind the second-last defender (not nearer to the goal line)."
               if f["margin_m"] < 0 else "At the moment of the pass, the attacker was exactly level with the second-last defender."]
        out.append("The attacker then played the ball." if f["attacker_involved"]
                   else "The attacker did not play the ball, interfere with an opponent or gain an advantage.")
        out.append(f"Measured by the {f.get('measured_by', 'offside line')}.")
        return out
    if fam == "handball":
        arm = {"by_side": "by his side", "natural_movement": "in a natural movement for the action",
               "extended_away": "extended away from his body", "raised_above_shoulder": "raised above his shoulder"}
        out = [f"The defender's arm was {arm.get(f['arm_position'], f['arm_position'].replace('_', ' '))}."]
        out.append("The ball came off the defender's own body onto the arm." if f["deflected_off_own_body"]
                   else "The ball came straight onto the arm, not off his own body.")
        out.append("He moved the arm towards the ball deliberately." if f["deliberate"] else "He did not move the arm towards the ball.")
        out.append(f"The ball travelled {f['distance_from_kick_m']:.1f} m from the kick to the arm.")
        return out
    if fam == "dogso":
        control = {"controlled": "had the ball under control", "likely": "was likely to keep control of the ball",
                   "unlikely": "was unlikely to keep control of the ball"}
        out = [f"The foul was {f['distance_to_goal_m']:.0f} m from goal, {f['angle_to_goal_deg']:.0f} degrees off the centre line, "
               f"{'inside' if f['in_penalty_area'] else 'outside'} the penalty area.",
               f"The attacker {control.get(f['ball_control'], f['ball_control'])}."]
        if f["covering_defender_can_reach"]:
            out.append(f"A covering defender could get there in time ({abs(f['cover_margin_s']):.1f} s to spare).")
        elif f.get("cover_margin_s", -9) <= -9:
            out.append("No defender was in a position to cover.")
        else:
            out.append(f"The nearest covering defender was {abs(f['cover_margin_s']):.1f} s too late to get there.")
        out.append("The offender was attempting to play the ball." if f["attempt_to_play_ball"]
                   else "The offender made no attempt to play the ball.")
        return out
    if fam == "holding":
        return [f"Holding for {f['duration_s']:.1f} s at a {f['set_piece'].replace('_', ' ')}.",
                "Both players were holding each other." if f["mutual"] else "Only the defender was holding.",
                "The hold stopped the attacker moving freely." if f["attacker_impeded"] else "The hold did not stop the attacker moving freely."]
    return [json.dumps(f)]


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
        "facts_established_on_review": facts_in_words(item),
        "decision_on_the_field": item["on_field_decision"],
        "allowed_decisions": DECISIONS[item["family"]],
        "referees_eye": measures,
    }, indent=1, default=str)


# --------------------------------------------------------------------------- the agent

def judge(item: dict) -> dict:
    """One incident: the agent's prediction in RefBench format."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": describe(item) + "\n\n" + GUIDANCE + "\n\nLaws of the Game:\n" + law_pages(item["family"])}]
    allowed = DECISIONS[item["family"]]
    answer: dict = {}
    for attempt in range(2):
        reply = _ask(messages)
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
    # The VAR protocol, in code: a borderline judgement is the referee's call, VAR stays out of it.
    intervene = bool(answer.get("should_var_intervene")) and decision != item["on_field_decision"] and not answer.get("borderline")
    if item["family"] == "offside":  # factual: a different answer from the measured line is always a VAR matter
        intervene = decision != item["on_field_decision"]
    return {"item_id": item["item_id"], "decision": decision, "should_var_intervene": intervene,
            "borderline": bool(answer.get("borderline")), "reason": answer.get("reason", ""),
            "answered": answer.get("decision") in allowed}


def _ask(messages: list):
    """One model call, waiting and retrying when the deployment's tokens-per-minute limit is hit."""
    from openai import RateLimitError

    for wait in (0, 15, 30, 60, 60, 60):
        time.sleep(wait)
        try:
            return client().chat.completions.create(model=MODEL_STRONG, messages=messages, max_completion_tokens=6000)
        except RateLimitError:
            continue
    return client().chat.completions.create(model=MODEL_STRONG, messages=messages, max_completion_tokens=6000)


SPLITS = ("tuning", "heldout")


def pick(items: list[dict], per_family: int, split: str = "tuning") -> list[dict]:
    """`tuning`: the first `per_family` incidents of each kind (the ones the prompt was written on).
    `heldout`: the next `per_family` of each kind, never seen while tuning. Same subset every run."""
    skip = 0 if split == "tuning" else per_family
    out, count = [], {}
    for it in items:
        k = count.get(it["family"], 0)
        if skip <= k < skip + per_family:
            out.append(it)
        count[it["family"]] = k + 1
    return out


def wilson(p: float | None, n: int, z: float = 1.96) -> list[float] | None:
    """95% confidence interval for a proportion (Wilson score)."""
    if p is None or not n:
        return None
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(c - h, 3), round(c + h, 3)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-family", type=int, default=20, help="incidents per kind (5 kinds); 100 = all 500")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--fresh", action="store_true", help="forget earlier answers and start again")
    ap.add_argument("--split", choices=SPLITS, default="tuning",
                    help="tuning = the incidents the prompt was written on; heldout = never seen")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.out is None:
        args.out = str(OUT if args.split == "tuning" else OUT.with_name("agents_heldout.jsonl"))

    items = [json.loads(l) for l in (DATA / "items.jsonl").read_text().splitlines()]
    todo = pick(items, args.per_family, args.split)
    out = Path(args.out)
    done = {}
    if args.fresh and out.exists():
        out.unlink()
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
    on_field = out.parent / ("on_field_same_items.jsonl" if args.split == "tuning" else "on_field_heldout.jsonl")
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
    n = ag["items_scored"]
    summary = {
        "split": args.split,
        "items": n,
        "agents_accuracy_ci95": wilson(ag["decision_accuracy"], n),
        "on_field_accuracy_ci95": wilson(of["decision_accuracy"], n),
        "on_field_accuracy": of["decision_accuracy"],
        "agents_accuracy": ag["decision_accuracy"],
        "facts": [["of clear errors flagged for VAR", pct(ag["var_recall"])],
                  ["of VAR calls justified", pct(ag["var_precision"])],
                  ["final decisions right, with VAR", pct(ag["final_accuracy"])]],
        "agents": ag, "on_field": of,
    }
    root = Path(__file__).resolve().parents[1]
    results = root / "refbench" / "results" / f"{args.split}.json"
    results.parent.mkdir(exist_ok=True)
    results.write_text(json.dumps(summary, indent=1))
    print(f"\nagents {pct(ag['decision_accuracy'])} (95% CI {summary['agents_accuracy_ci95']}), "
          f"on-field call {pct(of['decision_accuracy'])} (95% CI {summary['on_field_accuracy_ci95']})")
    print(f"Summary written to {results.relative_to(root)}")


if __name__ == "__main__":
    main()
