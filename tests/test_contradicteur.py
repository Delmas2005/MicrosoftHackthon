"""The Contradicteur's guarantees, as tests. No Azure needed: the model is faked.

Run: py -m pytest tests
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import contradicteur  # noqa: E402
from agents.card import MatchData, verify_card  # noqa: E402
from agents.fallback_narrator import checking_card, decided_card  # noqa: E402

DATA = MatchData(ROOT / "data" / "match_001")
INCIDENTS = [i["incident_id"] for i in DATA.match["incidents"]]


def decided(inc_id):
    settled = DATA.settle_time(inc_id)
    return decided_card(DATA, inc_id, {"period": settled[0], "t": settled[1] + 2.0})


def fake_model(answer=None, error=None):
    """A model that returns `answer` (a dict) or raises `error`, and remembers what it was shown."""
    seen = []

    def create(**kwargs):
        seen.append(kwargs["messages"][1]["content"])
        if error:
            raise error
        msg = SimpleNamespace(content=json.dumps(answer), tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), seen


def test_every_trap_passes_the_verifier():
    # The point of the traps: the numbers are right, so only the Contradicteur can catch them.
    for inc_id in INCIDENTS:
        card = contradicteur.plant_trap(decided(inc_id), DATA)
        assert "trap" in verify_card(card, DATA)["publishable_claims"], inc_id


def test_dropped_claims_are_removed(monkeypatch):
    card = contradicteur.plant_trap(decided("INC-01"), DATA)
    decisions = [{"id": c["id"], "decision": "keep"} for c in card["claims"] if c["id"] != "trap"]
    model, _ = fake_model({"claims": decisions + [{"id": "trap", "decision": "drop", "rule": 1, "reason": "overreach"}],
                           "referees_call": False})
    monkeypatch.setattr(contradicteur, "client", lambda: model)
    result = contradicteur.review_card(contradicteur.plant_trap(decided("INC-01"), DATA), DATA)
    assert "trap" not in [c["id"] for c in result["card"]["claims"]]
    assert result["review"]["dropped"][0]["id"] == "trap"


def test_it_can_never_add_or_rewrite(monkeypatch):
    card = decided("INC-01")
    keeps = [{"id": c["id"], "decision": "keep", "text": "rewritten"} for c in card["claims"]]
    model, _ = fake_model({"claims": [{"id": "new", "decision": "keep", "text": "Clear penalty."}] + keeps,
                           "referees_call": False})
    monkeypatch.setattr(contradicteur, "client", lambda: model)
    result = contradicteur.review_card(card, DATA)
    allowed = verify_card(card, DATA)["publishable_claims"]
    assert [c["id"] for c in result["card"]["claims"]] == allowed
    original = {c["id"]: c["text"] for c in card["claims"]}
    assert all(c["text"] == original[c["id"]] for c in result["card"]["claims"])


def test_model_failure_keeps_the_verifier_result(monkeypatch):
    model, _ = fake_model(error=TimeoutError("Foundry did not answer"))
    monkeypatch.setattr(contradicteur, "client", lambda: model)
    card = decided("INC-03")
    result = contradicteur.review_card(card, DATA)
    assert [c["id"] for c in result["card"]["claims"]] == verify_card(card, DATA)["publishable_claims"]
    assert result["review"]["status"].startswith("unavailable")


def test_checking_card_hides_the_outcome_from_the_reviewer(monkeypatch):
    model, seen = fake_model({"claims": [{"id": i, "decision": "keep"} for i in ("check", "vis", "blocker")],
                              "referees_call": True})
    monkeypatch.setattr(contradicteur, "client", lambda: model)
    inc = DATA.incident("INC-01")
    check_event = next(e for e in inc["linked_events"] if DATA.by_id[e]["type"] == "var_check")
    at = {"period": inc["period"], "t": DATA.by_id[check_event]["t"]}
    result = contradicteur.review_card(checking_card(DATA, "INC-01", at, check_event), DATA)
    assert "no_intervention" not in seen[0] and "no penalty" not in seen[0]
    assert "not made yet" in seen[0]
    assert result["review"]["referees_call"] is False  # only once the officials have decided


def test_a_claim_without_a_decision_is_asked_again_then_dropped(monkeypatch):
    answers = [{"claims": [{"id": "verdict", "decision": "keep"}], "referees_call": False}] * 2
    calls = []

    def create(**kwargs):
        calls.append(kwargs["messages"])
        msg = SimpleNamespace(content=json.dumps(answers[len(calls) - 1]), tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(contradicteur, "client", lambda: model)
    result = contradicteur.review_card(decided("INC-01"), DATA)
    assert len(calls) == 2 and "no keep/drop decision" in calls[1][-1]["content"]
    assert [c["id"] for c in result["card"]["claims"]] == ["verdict"]
