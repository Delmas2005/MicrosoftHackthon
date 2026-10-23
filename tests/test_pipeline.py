"""End to end: every card the live pipeline publishes passes the verifier untouched,
and no card is ever issued before the officials decide."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.card import MatchData, verify_card  # noqa: E402
from agents.fallback_narrator import checking_card, decided_card  # noqa: E402
from stream.replay import live_events, load_events  # noqa: E402
from stream.spotter import Spotter  # noqa: E402

DATA = MatchData(ROOT / "data" / "match_001")


def run_pipeline():
    sp, cards = Spotter(), []
    for e in live_events(load_events(ROOT / "data" / "match_001")):
        for s in sp.on_event(e):
            if s["signal"] == "var_check_started" and not s.get("review"):
                cards.append(checking_card(DATA, s["incident_id"], s["at"], s["event_id"]))
            elif s["signal"] == "decision_settled":
                cards += [decided_card(DATA, s["incident_id"], s["at"], a) for a in ("stadium", "tv")]
    return cards


def test_every_incident_gets_checking_and_decided_cards():
    cards = run_pipeline()
    ids = {i["incident_id"] for i in DATA.match["incidents"]}
    assert {c["incident_id"] for c in cards if c["phase"] == "checking"} == ids
    assert {c["incident_id"] for c in cards if c["phase"] == "decided"} == ids


def test_every_template_card_verifies_cleanly():
    for c in run_pipeline():
        v = verify_card(c, DATA)
        assert v["ok"], (c["incident_id"], c["phase"], v["errors"])


def test_decided_cards_never_precede_the_officials():
    for c in run_pipeline():
        if c["phase"] == "decided":
            assert (c["issued_at"]["period"], c["issued_at"]["t"]) >= DATA.settle_time(c["incident_id"])


def test_live_feed_hides_var_outcomes_until_complete():
    for e in live_events(load_events(ROOT / "data" / "match_001")):
        if e["type"] in ("var_check", "on_field_review"):
            assert "outcome" not in e and "duration_s" not in e


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)}/{len(tests)} pipeline tests passed")
