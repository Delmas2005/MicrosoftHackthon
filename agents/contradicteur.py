"""Contradicteur: attacks every claim of a decision card before it reaches a screen.

The deterministic verifier (agents/card.py) already checks the facts it can check by itself:
timing, evidence that exists, numbers that match. What it cannot check is meaning:
- "The referee could see 0% of the contact, so he got it wrong" has the right number and the
  wrong conclusion;
- a true quote from the Laws can be about another situation (inside instead of outside the area);
- a sentence can hint at a decision before the officials have made it.

That is the Contradicteur's job. It reads each claim with its evidence and decides `keep` or
`drop`. It can also flag the whole card as a "referee's call" when the evidence does not settle
the question either way. It can never add or rewrite a claim: it can only take things away.
Every claim needs an explicit decision: if the model skips one, it is asked again once, and a
claim still without a decision is dropped (nobody vetted it). If the model fails completely, the
card keeps exactly what the verifier allowed, so a broken model never adds anything.

Try it (from the repo root):
    py -m agents.contradicteur INC-01          # the template decided card of one incident
    py -m agents.contradicteur --all --traps   # all incidents, with a planted bad claim in each
"""
from __future__ import annotations

import argparse
import json

from agents.card import SETTLING_TYPES, MatchData, _lookup, verify_card
from agents.llm import MODEL_STRONG, client, parse_json

SYSTEM_PROMPT = """You are the Contradicteur of a football broadcast. Before a decision card is shown
to viewers, you attack every claim on it. For each claim you get its text and its evidence (the
measurement, event, Law or player it relies on). The numbers have already been checked: your job
is the meaning.

Drop a claim if any of these is true:
1. It says more than its evidence. Evidence about what the referee could see says nothing about
   whether the decision was right. "Could see 0% of the contact" is fine; "so he guessed" or "so
   he got it wrong" is not.
2. It judges or blames the officials, or suggests a decision was wrong, harsh, lucky or
   inexcusable. This includes the decision on the field, even when VAR later changed it: VAR
   correcting a call is the system working, not an excuse to criticise the referee. We explain
   what they could see and the Law they applied; we never overrule them. Advice on where the
   referee could have stood is fine when it is worded as coaching.
3. On a "checking" card (the officials have not decided yet): it hints at what the decision
   will be or should be, or applies the Law to this incident ("this was careless", "this arm made
   the body bigger"). A Law sentence that only states the general rule is allowed: it tells
   viewers what VAR is checking against. Keep it, even if the rule matches the type of incident.
4. A Law claim whose quote or explanation is about a different situation from this incident
   (for example a rule about offences inside the penalty area for an incident outside it).
5. It is unkind, sarcastic or partisan.

Keep every other claim. Do not drop a claim for style, length or because you would phrase it
differently: only for one of the five reasons above, and say which.

Give a decision for every claim id you receive, in the same order, even the ones you keep.

Set "referees_call" to true only if the facts on the card leave the question genuinely open
(for example the referee could see the incident clearly and the Law needs his judgement),
so viewers should be told it was a judgement call. Otherwise false.

Answer with this JSON object and nothing else:
{"claims": [{"id": "c1", "decision": "keep" or "drop", "rule": 0 to 5 (0 when kept), "reason": "short"}],
 "referees_call": false, "referees_call_reason": ""}"""


# --------------------------------------------------------------------------- what the model reads

def _evidence_for_review(ev: dict, data: MatchData, inc_id: str, phase: str) -> dict:
    """The evidence in readable form, with its true value (never the value the card claims)."""
    t = ev.get("type")
    if t == "measure":
        try:
            value = _lookup(data.analysis(inc_id), ev["path"])
        except (KeyError, IndexError, ValueError):
            value = "missing"
        return {"type": "measure", "what": ev["path"], "value": value}
    if t == "event":
        e = dict(data.by_id.get(ev.get("event_id", ""), {}))
        if phase == "checking" and e.get("type") in SETTLING_TYPES:
            e.pop("outcome", None)  # not decided yet: the reviewer must not see the outcome either
        for k in ("scripted_block", "possession"):
            e.pop(k, None)
        return {"type": "event", **e}
    if t == "law":
        return {k: v for k, v in ev.items() if k in ("type", "law", "page", "quote")}
    if t == "player":
        p = data.players.get(ev.get("id", ""), {})
        return {"type": "player", "id": ev.get("id"), "name": p.get("name"), "number": p.get("number")}
    return ev


def describe_card(card: dict, data: MatchData, claim_ids: list[str]) -> str:
    """What the model is told about the card: context, phase, and the claims to review."""
    from agents.laws_clerk import where  # same notion of "inside the area" as the Laws Clerk

    inc = data.incident(card["incident_id"])
    off = inc["officiating"]
    context = {
        "incident": inc["headline"],
        "type": inc["type"].replace("_", " "),
        "what the officials judge": inc["focal"]["what"],
        "where": where(inc),
        "decision on the field": off["on_field_decision"].replace("_", " "),
        "phase": card["phase"],
        "audience": card["audience"],
    }
    if card["phase"] == "decided" and card.get("decision"):
        context["officials' final decision"] = card["decision"]["value"].replace("_", " ")
    else:
        context["officials' final decision"] = "not made yet"
    claims = [{"id": c["id"], "kind": c["kind"], "text": c["text"],
               "evidence": [_evidence_for_review(e, data, card["incident_id"], card["phase"]) for e in c["evidence"]]}
              for c in card["claims"] if c["id"] in claim_ids]
    return json.dumps({"context": context, "claims": claims}, ensure_ascii=False, indent=1, default=str)


# --------------------------------------------------------------------------- the review

def review_card(card: dict, data: MatchData) -> dict:
    """Verifier first, then the Contradicteur on what the verifier let through.

    Returns {"card": the card to show, "verification": ..., "review": ...}. The card shown only
    ever has fewer claims than the input, never more or different ones."""
    verification = verify_card(card, data)
    allowed = verification["publishable_claims"]
    review = {"status": "skipped", "dropped": [], "referees_call": False}
    keep = list(allowed)

    if allowed:
        try:
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": describe_card(card, data, allowed)}]
            decisions: dict[str, dict] = {}
            for attempt in range(2):
                reply = client().chat.completions.create(model=MODEL_STRONG, messages=messages,
                                                         max_completion_tokens=8000)
                content = reply.choices[0].message.content
                answer = parse_json(content)
                for d in answer.get("claims", []):
                    # Only ids the verifier allowed count; anything else the model returns is ignored.
                    if isinstance(d, dict) and d.get("id") in allowed and d.get("decision") in ("keep", "drop"):
                        decisions[d["id"]] = d
                missing = [cid for cid in allowed if cid not in decisions]
                if not missing:
                    break
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": "You gave no keep/drop decision for: " + ", ".join(missing)
                              + ". Answer again with the full JSON object, with a decision for every claim."}]
            dropped = [{"id": cid, "rule": decisions[cid].get("rule"), "reason": decisions[cid].get("reason", "")}
                       for cid in allowed if decisions.get(cid, {}).get("decision") == "drop"]
            dropped += [{"id": cid, "rule": None, "reason": "no decision from the Contradicteur: not vetted"}
                        for cid in allowed if cid not in decisions]
            keep = [cid for cid in allowed if cid not in {d["id"] for d in dropped}]
            review = {"status": "done", "dropped": dropped,
                      "referees_call": bool(answer.get("referees_call")) and card["phase"] == "decided",
                      "referees_call_reason": answer.get("referees_call_reason", "")}
        except Exception as exc:  # timeout, quota, bad JSON: keep what the verifier allowed
            review = {"status": f"unavailable ({type(exc).__name__}), verifier result kept",
                      "dropped": [], "referees_call": False}

    shown = dict(card)
    shown["claims"] = [c for c in card["claims"] if c["id"] in keep]
    if review["referees_call"]:
        shown["framing"] = "referees_call"  # the screens show it as a badge, not as a new sentence
    return {"card": shown, "verification": verification, "review": review}


# --------------------------------------------------------------------------- planted mistakes, for the demo

def plant_trap(card: dict, data: MatchData) -> dict:
    """Add a claim that passes the verifier (its numbers are right) but says more than its evidence."""
    vis = next((c for c in card["claims"] if c["id"] == "vis"), None)
    if vis is None:
        return card
    trapped = json.loads(json.dumps(card))
    value = next(e["value"] for e in vis["evidence"] if e["path"].endswith("visible_share_at_key_moment"))
    who = "assistant referee" if ".AR1." in vis["evidence"][0]["path"] else "referee"
    blame = ("so the decision on the field was a guess" if value < 0.5
             else "so there is no excuse for the decision on the field")
    trapped["claims"].append({
        "id": "trap", "kind": "fact",
        "text": f"The {who} could see {value * 100:.0f}% of it, {blame}.",
        "evidence": [e for e in vis["evidence"] if e["path"].endswith("visible_share_at_key_moment")],
    })
    return trapped


def main():
    from agents.fallback_narrator import decided_card

    ap = argparse.ArgumentParser()
    ap.add_argument("incident", nargs="?", default="INC-01")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--traps", action="store_true", help="add a planted bad claim to each card")
    ap.add_argument("--match", default="data/match_001")
    args = ap.parse_args()
    data = MatchData(args.match)
    ids = [i["incident_id"] for i in data.match["incidents"]] if args.all else [args.incident]

    caught = planted = 0
    for inc_id in ids:
        settled = data.settle_time(inc_id)
        card = decided_card(data, inc_id, {"period": settled[0], "t": settled[1] + 2.0})
        if args.traps:
            card = plant_trap(card, data)
        result = review_card(card, data)
        print(f"\n=== {inc_id}: {data.incident(inc_id)['headline']}")
        print(f"  verifier let through: {result['verification']['publishable_claims']}")
        print(f"  contradicteur: {result['review']['status']}")
        for d in result["review"]["dropped"]:
            print(f"    dropped {d['id']} (rule {d['rule']}): {d['reason']}")
        if result["review"]["referees_call"]:
            print(f"    referee's call: {result['review']['referees_call_reason']}")
        print("  shown:")
        for c in result["card"]["claims"]:
            print(f"    - {c['text']}")
        if any(c["id"] == "trap" for c in card["claims"]):
            planted += 1
            caught += any(d["id"] == "trap" for d in result["review"]["dropped"])
    if planted:
        print(f"\nPlanted mistakes caught: {caught}/{planted}")


if __name__ == "__main__":
    main()
