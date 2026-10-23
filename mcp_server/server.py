"""Fourth Official MCP server: the tools every agent calls.

Agents reason; these tools measure. Nothing an agent shows on screen may come from anywhere else.

Run (stdio, for Agent Framework / Copilot / Claude):   python -m mcp_server.server
Run (HTTP, for agents hosted on Azure Container Apps): python -m mcp_server.server --http --port 8080

Data: FOURTH_OFFICIAL_MATCH (default data/match_001) and FOURTH_OFFICIAL_REFBENCH (default refbench/data).
"""
from __future__ import annotations

import argparse
import json
import os
from functools import lru_cache
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from agents.card import MatchData, verify_card as _verify
from refbench.adapter import analyze_item

ROOT = Path(__file__).resolve().parents[1]
MATCH_DIR = Path(os.environ.get("FOURTH_OFFICIAL_MATCH", ROOT / "data" / "match_001"))
REFBENCH_DIR = Path(os.environ.get("FOURTH_OFFICIAL_REFBENCH", ROOT / "refbench" / "data"))

server = MCPServer(
    name="fourth-official",
    title="Fourth Official: Referee's Eye tools",
    instructions=(
        "Measurement tools for explaining football refereeing decisions. Every number you put in a "
        "decision card must come from one of these tools, cited by its path. Never state a decision "
        "before get_officiating_status says the officials have settled it. Always run verify_card "
        "before publishing; only publish the claims it returns as publishable."
    ),
    version="0.2.0",
)


@lru_cache(maxsize=1)
def data() -> MatchData:
    return MatchData(MATCH_DIR)


@lru_cache(maxsize=1)
def refbench_items() -> dict:
    p = REFBENCH_DIR / "items.jsonl"
    return {r["item_id"]: r for r in (json.loads(l) for l in p.read_text().splitlines())} if p.exists() else {}


def _strip_frames(a: dict) -> dict:
    a = json.loads(json.dumps(a))
    for o in a.get("observers", {}).values():
        o.pop("frames", None)
    a.pop("incident_occupancy", None)
    return a


@server.tool()
def list_incidents() -> list[dict]:
    """All refereeing incidents in the match: id, clock, type, neutral headline.
    Outcomes are not included: ask get_officiating_status at a match time."""
    return [{k: v for k, v in i.items() if k != "final_decision"} for i in data().match["incidents"]]


@server.tool()
def get_incident(incident_id: str) -> dict:
    """What a live system knows at the moment of an incident: who, where, the live call, what VAR is
    checking, the Laws that apply. Outcomes are withheld (see get_officiating_status).
    Positions are left out; use the measurement tools for geometry."""
    inc = json.loads(json.dumps(data().incident(incident_id)))
    inc.pop("window", None)
    off = inc["officiating"]
    inc["officiating"] = {"on_field_decision": off["on_field_decision"], "var_checking": off["var"].get("check")}
    return inc


@server.tool()
def get_officiating_status(incident_id: str, period: int, t: float) -> dict:
    """At match time (period, t): has the officials' decision on this incident been settled yet?
    A decided card may only be issued once settled is true."""
    d = data()
    settled = d.settle_time(incident_id)
    inc = d.incident(incident_id)
    is_settled = settled is not None and (period, t) >= settled
    out = {"incident_id": incident_id, "settled": is_settled, "settled_at": settled,
           "on_field_decision": inc["officiating"]["on_field_decision"]}
    if is_settled:
        off = inc["officiating"]
        out["final_decision"] = off["final_decision"]
        out["var_outcome"] = off["var"].get("outcome")
        out["stadium_announcement"] = off.get("stadium_announcement")
        out["restart"] = off.get("restart")
        src = [e for e in inc["linked_events"] if d.by_id[e]["type"] in ("decision", "on_field_review", "var_check", "card")]
        out["decision_source_event"] = src[-1] if src else None
    return out


@server.tool()
def get_events(period: int, t_from: float, t_to: float, types: list[str] | None = None) -> list[dict]:
    """Events from the live feed between two match times (seconds since the start of the half)."""
    return [e for e in data().events if e["period"] == period and t_from <= e["t"] <= t_to and (not types or e["type"] in types)]


@server.tool()
def referee_eye(incident_id: str) -> dict:
    """Full Referee's Eye measurement for an incident: what each official could see (visibility 0-1,
    blockers, distance, side-on angle), best reachable viewing spot, and incident-specific checks
    (offside geometry, covering defenders, body angle, attack value). Cite fields by their path."""
    return _strip_frames(data().analysis(incident_id))


@server.tool()
def visibility_timeline(incident_id: str, observer: str = "REF") -> list[dict]:
    """Frame by frame: how much of the key point the official could see, and which players hid it."""
    return data().analysis(incident_id)["observers"][observer]["frames"]


@server.tool()
def best_view(incident_id: str) -> dict:
    """Where the referee could have stood (within running reach) to see the incident best. Coaching, not blame."""
    return data().analysis(incident_id)["best_view"]


@server.tool()
def attack_value(incident_id: str) -> dict:
    """What the attack was worth at the key moment: numbers around the ball and chance value (simple model)."""
    return data().analysis(incident_id)["attack"]


@server.tool()
def added_time(period: int) -> dict:
    """Dead-ball breakdown and announced added time for a half."""
    st = json.loads((MATCH_DIR / "stoppages.json").read_text())
    return st["by_period"][str(period)]


@server.tool()
def verify_card(card: dict) -> dict:
    """Check a decision card before it is shown: no decision before the officials, every claim backed
    by evidence, every number in the text matching that evidence. Returns publishable claim ids."""
    return _verify(card, data())


@server.tool()
def refbench_get_item(item_id: str) -> dict:
    """A RefBench evaluation item (facts and on-field call), without positions."""
    it = dict(refbench_items()[item_id])
    it.pop("window", None)
    return it


@server.tool()
def refbench_referee_eye(item_id: str) -> dict:
    """Referee's Eye measurements for a RefBench item."""
    return _strip_frames(analyze_item(refbench_items()[item_id]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    if args.http:
        server.run(transport="streamable-http", host="0.0.0.0", port=args.port)
    else:
        server.run()


if __name__ == "__main__":
    main()
