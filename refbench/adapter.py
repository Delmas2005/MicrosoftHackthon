"""Run the Referee's Eye tools on a RefBench item (same code path as the live match)."""
from __future__ import annotations

from tools.referee_eye import analyze_from_files

TYPE = {"penalty_contact": "penalty_claim", "offside": "offside_goal", "handball": "handball",
        "dogso": "dogso", "holding": "penalty_claim_holding"}


def as_match_and_incident(item: dict):
    d = item["attack_direction"]
    match = {
        "attack_direction": {"1": {item["attacking_team"]: d, item["defending_team"]: -d}},
        "teams": {
            item["attacking_team"]: {"starting": {}},
            item["defending_team"]: {"starting": {g: {"slot": "GK"} for g in item.get("goalkeepers", [])}},
        },
    }
    incident = {
        "incident_id": item["item_id"],
        "period": 1,
        "type": TYPE[item["family"]],
        "attacking_team": item["attacking_team"],
        "defending_team": item["defending_team"],
        "involved": {role: {"id": pid} for role, pid in item["involved"].items()},
        "focal": item["focal"],
        "critical_window": item["critical_window"],
        "observers": {o: o for o in item["observers"]},
        "window": item["window"],
    }
    if "facing_target" in item:
        incident["facing_target"] = item["facing_target"]
    return match, incident


def analyze_item(item: dict) -> dict:
    return analyze_from_files(*as_match_and_incident(item))
