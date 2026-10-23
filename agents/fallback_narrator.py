"""Template narrator: builds decision cards straight from the measurement tools, no LLM.

Two jobs:
1. Safety net. If the model endpoint is slow or down, or the Contradicteur rejects every LLM
   claim, the screen still gets a correct, verified card. (Failure recovery the jury can see.)
2. Reference. Every LLM card can be compared against this one.

English only. The LLM Narrator does languages, tone and personalisation.
"""
from __future__ import annotations

from agents.card import MatchData

LAW_LINE = {
    "penalty_claim": ("Law 12", "Law 12: contact on its own is not a foul. The challenge has to be careless, reckless or use excessive force."),
    "offside_goal": ("Law 11", "Law 11: a player is offside if he is beyond the second-last defender when a team-mate plays the ball and he then plays it."),
    "handball": ("Law 12", "Law 12: it is handball if the arm makes the body unnaturally bigger."),
    "dogso": ("Law 12", "Law 12: denying an obvious goal-scoring opportunity outside the area is a sending-off offence."),
    "penalty_claim_holding": ("Law 12", "Law 12: holding is an offence when it stops an opponent moving freely."),
}
CHECK_TEXT = {
    "possible_penalty": "a possible penalty",
    "offside_semi_automated": "offside, with the semi-automated offside line",
    "possible_handball_penalty": "a possible handball penalty",
    "possible_red_card_dogso": "a possible red card for denying a goal-scoring opportunity",
    "possible_penalty_holding": "a possible penalty for holding",
}
DECISION_TEXT = {
    "no_penalty": "No penalty.",
    "goal_disallowed_offside": "Goal disallowed for offside.",
    "penalty": "Penalty.",
    "red_card": "Red card.",
}


def _m(path, analysis):
    cur = analysis
    for p in path.split("."):
        cur = cur[p]
    return {"type": "measure", "path": path, "value": cur}, cur


def _visibility_claim(d: MatchData, inc_id: str, observer: str = "REF"):
    a = d.analysis(inc_id)
    inc = d.incident(inc_id)
    o = a["observers"][observer]
    ev_d, dist = _m(f"observers.{observer}.distance_to_focal_m", a)
    ev_v, vis = _m(f"observers.{observer}.visible_share_at_key_moment", a)
    who = "The referee" if observer == "REF" else "The assistant referee"
    text = f"{who} was {dist:.0f} m away and could see {vis * 100:.0f}% of {inc['focal']['what']}."
    claims = [{"id": "vis", "kind": "fact", "text": text, "evidence": [ev_d, ev_v]}]
    if o["blockers"] and vis < 0.5:
        b = o["blockers"][0]
        p = d.players[b]
        claims.append({"id": "blocker", "kind": "fact",
                       "text": f"{p['name']}, number {p['number']}, was in his line of sight.",
                       "evidence": [{"type": "player", "id": b},
                                    {"type": "measure", "path": f"observers.{observer}.blocker_mean_cover.{b}",
                                     "value": o["blocker_mean_cover"][b]}]})
    return claims


def checking_card(d: MatchData, inc_id: str, at: dict, check_event: str, audience: str = "tv") -> dict:
    inc = d.incident(inc_id)
    check = inc["officiating"]["var"].get("check")
    claims = [{"id": "check", "kind": "fact", "text": f"VAR is checking {CHECK_TEXT.get(check, 'the incident')}.",
               "evidence": [{"type": "event", "event_id": check_event}]}]
    claims += _visibility_claim(d, inc_id, "AR1" if inc["type"] == "offside_goal" else "REF")
    return {"incident_id": inc_id, "phase": "checking", "issued_at": {"period": at["period"], "t": at["t"]},
            "audience": audience, "language": "en", "decision": None, "claims": claims, "author": "template"}


def decided_card(d: MatchData, inc_id: str, at: dict, audience: str = "tv") -> dict:
    inc = d.incident(inc_id)
    a = d.analysis(inc_id)
    final = inc["officiating"]["final_decision"]
    src = [e for e in inc["linked_events"] if d.by_id[e]["type"] in ("decision", "on_field_review", "var_check", "card")][-1]
    law, law_text = LAW_LINE[inc["type"]]
    claims = [
        {"id": "verdict", "kind": "verdict", "text": DECISION_TEXT.get(final, final.replace("_", " ").capitalize() + "."),
         "evidence": [{"type": "event", "event_id": src}]},
        {"id": "law", "kind": "law", "text": law_text, "evidence": [{"type": "law", "law": law}]},
    ]
    if audience == "stadium":
        return {"incident_id": inc_id, "phase": "decided", "issued_at": {"period": at["period"], "t": at["t"]},
                "audience": audience, "language": "en", "decision": {"value": final, "source_event": src},
                "claims": claims, "author": "template"}
    t = inc["type"]
    if t == "offside_goal":
        e1, margin = _m("offside.margin_m", a)
        e2, deg = _m("offside.assistant_referee.margin_as_seen_deg", a)
        claims.append({"id": "margin", "kind": "fact",
                       "text": f"Offside by {margin * 100:.0f} cm. From the touchline that gap was {deg:.1f} degrees wide: too small to see at full speed.",
                       "evidence": [e1, e2]})
    elif t == "dogso":
        e1, ca = _m("dogso_cover.closest_attempt", a)
        e2, dist = _m("dogso_cover.distance_to_goal_m", a)
        p = d.players[ca["defender"]]
        claims.append({"id": "cover", "kind": "fact",
                       "text": f"{dist:.0f} m from goal and no cover: the best-placed defender, {p['name']}, was {abs(ca['best_margin_s']):.1f} s too late to get across.",
                       "evidence": [e1, e2, {"type": "player", "id": ca["defender"]}]})
    elif t == "handball":
        e1, ang = _m("behind_body.angle_from_defender_facing_deg", a)
        claims.append({"id": "body", "kind": "fact",
                       "text": f"The referee was {ang:.0f} degrees behind the defender, so the arm was on the far side of his body.",
                       "evidence": [e1]})
    claims += _visibility_claim(d, inc_id, "AR1" if t == "offside_goal" else "REF")
    bv = a["best_view"]
    if not bv["already_good"]:
        e1, left = _m("best_view.move_m.left", a)
        e2, closer = _m("best_view.move_m.closer", a)
        parts = []
        if abs(left) >= 1:
            parts.append(f"{abs(left):.0f} m to his {'left' if left > 0 else 'right'}")
        if abs(closer) >= 1:
            parts.append(f"{abs(closer):.0f} m {'closer' if closer > 0 else 'further back'}")
        why = ""
        if bv["best"]["visible_share"] - (bv["actual"]["visible_share"] or 0) < 0.05:
            why = " for a side-on view instead of looking along the run"
        claims.append({"id": "best_view", "kind": "context",
                       "text": f"Best view within reach: {' and '.join(parts)}{why}.", "evidence": [e1, e2]})
    return {"incident_id": inc_id, "phase": "decided", "issued_at": {"period": at["period"], "t": at["t"]},
            "audience": audience, "language": "en", "decision": {"value": final, "source_event": src},
            "claims": claims, "author": "template"}
