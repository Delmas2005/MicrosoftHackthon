"""Incident Spotter: watches the live feed and says when to brief and when to explain.

Signals
-------
incident_detected   a reviewable moment happened (contact, arm, grappling, foul tagged as an incident)
var_check_started   VAR is checking: time for a 'checking' card (facts only, no verdict)
decision_settled    the officials have decided: time for a 'decided' card

It only uses what has already arrived on the feed. It never looks ahead.
"""
from __future__ import annotations


class Spotter:
    def __init__(self):
        self.open: dict[str, dict] = {}       # incident -> state
        self.settled: set[str] = set()

    def on_event(self, e: dict) -> list[dict]:
        out = []
        inc = e.get("incident")
        if not inc:
            return out
        st = self.open.setdefault(inc, {"detected": False, "checks_running": 0})
        at = {"period": e["period"], "t": e["t"], "clock": e["clock"]}
        if not st["detected"]:
            st["detected"] = True
            out.append({"signal": "incident_detected", "incident_id": inc, "at": at, "event_id": e["event_id"]})
        if e["type"] in ("var_check", "on_field_review") and e.get("status") == "started":
            st["checks_running"] += 1
            out.append({"signal": "var_check_started", "incident_id": inc, "at": at, "event_id": e["event_id"],
                        "check": e.get("check"), "review": e["type"] == "on_field_review"})
        if e["type"] in ("var_check_complete", "on_field_review_complete"):
            st["checks_running"] -= 1
        # a check that ends by sending the referee to the screen is not the end of the story
        final = (e["type"] == "var_check_complete" and e.get("outcome") != "on_field_review_recommended") \
            or e["type"] == "decision" or (e["type"] == "card" and e.get("card") == "red")
        if final and st["checks_running"] == 0 and inc not in self.settled:
            self.settled.add(inc)
            out.append({"signal": "decision_settled", "incident_id": inc, "at": at, "event_id": e["event_id"]})
        return out
