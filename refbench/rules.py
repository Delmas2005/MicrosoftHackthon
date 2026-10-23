"""RefBench decision rules (v0.1).

A deliberately small, explicit reading of IFAB Law 11 (offside) and Law 12 (fouls, handball,
DOGSO, holding). It is NOT the Laws of the Game: it is the answer key for a synthetic benchmark,
written so every label can be traced to a rule. Each function returns:

    (correct_decision, borderline, key_factors)

`borderline` marks cases where reasonable officials could differ. VAR does not intervene on a
borderline subjective call ("clear and obvious" threshold); factual calls (offside) are never borderline.
"""
from __future__ import annotations


def penalty_contact(f: dict):
    bf, force, after, point = f["ball_played_first"], f["contact_force"], f.get("contact_after_ball_s"), f["contact_point"]
    if not bf:
        if force == "low" and point == "upper_body":
            return "no_penalty", True, ["defender did not play the ball", "only light upper-body contact"]
        return "penalty", force == "low", ["defender did not play the ball", f"{force} contact on the {point.replace('_', ' ')}"]
    late = after is not None and after >= 0.3
    if force == "high" and late:
        return "penalty", False, ["ball played first", f"high-force follow-through {after:.2f} s later"]
    borderline = (force == "high" and after is not None and after >= 0.15) or (force == "medium" and late)
    return "no_penalty", borderline, ["ball played first", f"{force} follow-through {after:.2f} s after the ball"]


def offside(f: dict):
    if f["margin_m"] > 0 and f["attacker_involved"]:
        return "offside", False, [f"attacker {f['margin_m'] * 100:.0f} cm beyond the second-last defender", "attacker plays the ball"]
    if f["margin_m"] > 0:
        return "no_offside", False, ["in an offside position but not involved in play"]
    return "no_offside", False, [f"attacker level or behind by {abs(f['margin_m']) * 100:.0f} cm"]


def handball(f: dict):
    arm, defl, dist, delib = f["arm_position"], f["deflected_off_own_body"], f["distance_from_kick_m"], f["deliberate"]
    bigger = arm in ("raised_above_shoulder", "extended_away")
    if delib:
        return "penalty", False, ["deliberate handling"]
    if bigger and not defl:
        return "penalty", arm == "extended_away" and dist < 4, [f"arm {arm.replace('_', ' ')}: body made unnaturally bigger", f"ball travelled {dist:.1f} m"]
    if bigger and defl:
        return "no_penalty", True, ["ball deflected off the player's own body onto the arm"]
    return "no_penalty", False, [f"arm {arm.replace('_', ' ')}: natural position"]


def dogso(f: dict):
    d, a, ctrl, cover = f["distance_to_goal_m"], f["angle_to_goal_deg"], f["ball_control"], f["covering_defender_can_reach"]
    obvious = d <= 30 and a <= 35 and ctrl != "unlikely" and not cover
    border = 27 <= d <= 33 or 30 <= a <= 40 or abs(f.get("cover_margin_s", 9)) < 0.2
    factors = [f"{d:.0f} m from goal", f"{a:.0f} degrees off centre", f"ball control {ctrl}",
               "no covering defender could get there" if not cover else "a covering defender could get there"]
    if obvious:
        if f["in_penalty_area"] and f["attempt_to_play_ball"]:
            return "yellow_card", border, factors + ["in the area with a genuine attempt to play the ball: penalty plus yellow"]
        return "red_card", border, factors
    return "yellow_card", border, factors + ["not an obvious goal-scoring opportunity: stopping a promising attack"]


def holding(f: dict):
    dur, mutual, imp = f["duration_s"], f["mutual"], f["attacker_impeded"]
    border = 0.4 <= dur <= 0.9 or (mutual and imp)
    if not mutual and imp and dur >= 0.6:
        return "penalty", border, [f"defender holds for {dur:.1f} s", "attacker clearly impeded"]
    why = "both players holding" if mutual else ("attacker not impeded" if not imp else f"holding only {dur:.1f} s")
    return "no_penalty", border, [why]


RULES = {"penalty_contact": penalty_contact, "offside": offside, "handball": handball, "dogso": dogso, "holding": holding}
LAW = {"penalty_contact": "Law 12", "offside": "Law 11", "handball": "Law 12", "dogso": "Law 12", "holding": "Law 12"}
ALTERNATIVE = {
    "penalty": "no_penalty", "no_penalty": "penalty",
    "offside": "no_offside", "no_offside": "offside",
    "red_card": "yellow_card", "yellow_card": "red_card",
}


def decide(family: str, facts: dict):
    return RULES[family](facts)
