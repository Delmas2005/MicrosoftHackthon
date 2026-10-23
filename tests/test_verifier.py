"""The guardrails, as tests. Run: python -m pytest tests  (or python tests/test_verifier.py)."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.card import MatchData, verify_card  # noqa: E402

DATA = MatchData(ROOT / "data" / "match_001")


def ev(inc, type_):
    return next(e for e in DATA.incident(inc)["linked_events"] if DATA.by_id[e]["type"] == type_)


def good_card():
    settle = DATA.settle_time("INC-01")
    return {
        "incident_id": "INC-01",
        "phase": "decided",
        "issued_at": {"period": settle[0], "t": settle[1] + 2.0},
        "audience": "tv",
        "language": "en",
        "decision": {"value": "no_penalty", "source_event": ev("INC-01", "var_check")},
        "claims": [
            {"id": "c1", "kind": "fact",
             "text": "From 20 m away the referee could see 0% of the contact: number 5 was in his line of sight.",
             "evidence": [
                 {"type": "measure", "path": "observers.REF.distance_to_focal_m", "value": 20.0},
                 {"type": "measure", "path": "observers.REF.visible_share_at_key_moment", "value": 0.0},
                 {"type": "player", "id": "NBR-05"}]},
            {"id": "c2", "kind": "law", "text": "Law 12: the defender played the ball before the contact.",
             "evidence": [{"type": "law", "law": "Law 12"}, {"type": "event", "event_id": ev("INC-01", "tackle")}]},
            {"id": "c3", "kind": "verdict", "text": "VAR checked it and did not intervene. No penalty.",
             "evidence": [{"type": "event", "event_id": ev("INC-01", "var_check")}]},
            {"id": "c4", "kind": "context", "text": "8 m to his right and 4 m closer, he would have seen it.",
             "evidence": [{"type": "measure", "path": "best_view.move_m.left", "value": -8.4},
                          {"type": "measure", "path": "best_view.move_m.closer", "value": 4.2}]},
        ],
    }


def test_good_card_passes():
    r = verify_card(good_card(), DATA)
    assert r["ok"], r["errors"]
    assert r["publishable_claims"] == ["c1", "c2", "c3", "c4"]


def test_decision_before_officials_is_blocked():
    c = good_card()
    c["issued_at"]["t"] = DATA.key_moment("INC-01")[1] + 5.0
    r = verify_card(c, DATA)
    assert not r["ok"] and r["publishable_claims"] == []
    assert any(e["rule"] == "anticipation" for e in r["errors"])


def test_checking_card_cannot_carry_a_decision():
    c = good_card()
    c["phase"] = "checking"
    c["issued_at"]["t"] = DATA.key_moment("INC-01")[1] + 5.0
    r = verify_card(c, DATA)
    assert not r["ok"] and r["publishable_claims"] == []


def test_verdict_claim_not_allowed_while_checking():
    c = good_card()
    c["phase"], c["decision"] = "checking", None
    c["issued_at"]["t"] = DATA.key_moment("INC-01")[1] + 5.0
    r = verify_card(c, DATA)
    assert "c3" in r["rejected_claims"] and "c1" in r["publishable_claims"]


def test_wrong_decision_value_is_blocked():
    c = good_card()
    c["decision"]["value"] = "penalty"
    assert not verify_card(c, DATA)["ok"]


def test_invented_number_is_rejected():
    c = good_card()
    c["claims"][0]["text"] = "The referee could see 45% of the contact from 20 m."
    r = verify_card(c, DATA)
    assert "c1" in r["rejected_claims"] and "c2" in r["publishable_claims"]
    assert any(e["rule"] == "number" for e in r["errors"])


def test_wrong_measurement_is_rejected():
    c = good_card()
    c["claims"][0]["evidence"][1]["value"] = 0.6
    r = verify_card(c, DATA)
    assert "c1" in r["rejected_claims"]


def test_claim_without_evidence_is_rejected():
    c = good_card()
    c["claims"].append({"id": "c5", "kind": "fact", "text": "The referee was badly placed.", "evidence": []})
    r = verify_card(c, DATA)
    assert "c5" in r["rejected_claims"]


def test_wrong_law_is_rejected():
    c = good_card()
    c["claims"][1]["evidence"][0]["law"] = "Law 11"
    c["claims"][1]["text"] = "Law 11 applies here."
    r = verify_card(c, DATA)
    assert "c2" in r["rejected_claims"]


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)}/{len(tests)} verifier tests passed")
