"""The Narrator's guarantees, as tests. No Azure needed: the models are faked.

Run: py -m pytest tests
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import contradicteur, narrator  # noqa: E402
from agents.card import MatchData  # noqa: E402
from agents.fallback_narrator import decided_card  # noqa: E402

DATA = MatchData(ROOT / "data" / "match_001")


def tv_card(inc_id="INC-01"):
    settled = DATA.settle_time(inc_id)
    return decided_card(DATA, inc_id, {"period": settled[0], "t": settled[1] + 2.0}, "tv")


def model_saying(*answers):
    """A narrator model that gives these answers one after the other."""
    answers = list(answers)

    def create(**kwargs):
        content = json.dumps(answers.pop(0)) if answers else "{}"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def keep_everything():
    def create(**kwargs):
        ids = [c["id"] for c in json.loads(kwargs["messages"][1]["content"])["claims"]]
        answer = {"claims": [{"id": i, "decision": "keep"} for i in ids], "referees_call": False}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


GOOD_FAN = {"claims": [
    {"text": "No hay penalti.", "sources": ["verdict"]},
    {"text": "El árbitro estaba a 20 m y veía el 0% del contacto.", "sources": ["vis"]},
]}


def test_good_fan_card_is_published(monkeypatch):
    monkeypatch.setattr(narrator, "client", lambda: model_saying(GOOD_FAN))
    monkeypatch.setattr(contradicteur, "client", keep_everything)
    r = narrator.narrate(tv_card(), DATA, "fan", "es")
    assert r["published"], r["problems"]
    card = r["card"]
    assert card["language"] == "es" and card["club"] == "Kestrel Rovers" and card["revision"] == 3
    assert card["claims"][0]["kind"] == "verdict"
    assert card["claims"][1]["evidence"] == next(c for c in tv_card()["claims"] if c["id"] == "vis")["evidence"]


def test_a_wrong_number_is_never_shown(monkeypatch):
    wrong = {"claims": [{"text": "El árbitro veía el 10% del contacto.", "sources": ["vis"]}]}
    monkeypatch.setattr(narrator, "client", lambda: model_saying(wrong))
    monkeypatch.setattr(contradicteur, "client", keep_everything)
    assert not narrator.narrate(tv_card(), DATA, "fan", "es")["published"]


def test_numbers_in_words_are_refused():
    words = {"claims": [{"text": "El árbitro estaba a veinte metros.", "sources": ["vis"]}]}
    problems = narrator.check_narration(words, tv_card(), "fan", "es")
    assert any("digits" in p for p in problems)


def test_every_sentence_needs_a_known_source(monkeypatch):
    invented = {"claims": [{"text": "Clear dive.", "sources": []}]}
    monkeypatch.setattr(narrator, "client", lambda: model_saying(invented, invented))
    r = narrator.narrate(tv_card(), DATA, "stadium")
    assert not r["published"] and any("sources" in p for p in r["problems"])
    assert narrator.check_narration({"claims": [{"text": "x", "sources": ["nope"]}]}, tv_card(), "stadium", "en")


def test_stadium_line_must_be_short():
    long_line = {"claims": [{"text": " ".join(["word"] * 20), "sources": ["verdict"]}]}
    assert any("words" in p for p in narrator.check_narration(long_line, tv_card(), "stadium", "en"))


def test_one_fix_then_published(monkeypatch):
    words = {"claims": [{"text": "No penalty: the referee was twenty metres away.", "sources": ["verdict", "vis"]}]}
    fixed = {"claims": [{"text": "No penalty. The referee was 20 m away.", "sources": ["verdict", "vis"]}]}
    model = model_saying(words, fixed)  # one model for both calls: it answers wrong, then right
    monkeypatch.setattr(narrator, "client", lambda: model)
    monkeypatch.setattr(contradicteur, "client", keep_everything)
    assert narrator.narrate(tv_card(), DATA, "stadium")["published"]


def test_aggrieved_club_follows_the_decision():
    assert narrator.aggrieved_club(DATA, "INC-01") == "Kestrel Rovers"   # no penalty for Kestrel
    assert narrator.aggrieved_club(DATA, "INC-02") == "Kestrel Rovers"   # Kestrel goal disallowed
    assert narrator.aggrieved_club(DATA, "INC-03") == "Northbridge FC"   # penalty against Northbridge
    assert narrator.aggrieved_club(DATA, "INC-04") == "Kestrel Rovers"   # red card for Kestrel


def test_badge_comes_from_the_tv_card(monkeypatch):
    source = tv_card()
    source["framing"] = "referees_call"
    monkeypatch.setattr(narrator, "client", lambda: model_saying({"claims": [{"text": "No penalty.", "sources": ["verdict"]}]}))
    monkeypatch.setattr(contradicteur, "client", keep_everything)
    assert narrator.narrate(source, DATA, "stadium")["card"]["framing"] == "referees_call"
