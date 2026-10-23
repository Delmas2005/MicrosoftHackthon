"""The Laws Clerk's safety check, as tests. No Azure needed: the model and the search are faked.

Run: py -m pytest tests
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import laws_clerk  # noqa: E402
from agents.card import MatchData, verify_card  # noqa: E402
from agents.fallback_narrator import LAW_LINE, decided_card  # noqa: E402

DATA = MatchData(ROOT / "data" / "match_001")
HANDBALL = DATA.incident("INC-03")

# A page as the search index returns it (text shortened, with the PDF's odd spacing).
PAGE = {"page": 56, "section": "Law 12", "text": (
    "110 • holds an opponent • impedes an opponent with contact Handling the ball "
    "It is an offence if a player: • deliberately touches the ball with their hand/arm, "
    "for example moving the hand/ arm towards the ball • touches the ball with their hand/arm "
    "when it has made their body unnaturally bigger.")}
GOOD = {"law": "Law 12", "clause": "Handling the ball", "page": 56,
        "quote": "touches the ball with their hand/arm when it has made their body unnaturally bigger",
        "explanation": "It is handball if the arm makes the body bigger in a way that is not natural."}


def test_good_citation_passes():
    assert laws_clerk.check_citation(GOOD, HANDBALL, {56: PAGE}) == []


def test_quote_must_be_word_for_word():
    bad = dict(GOOD, quote="touches the ball with their arm when it makes their body much bigger")
    assert any("word for word" in p for p in laws_clerk.check_citation(bad, HANDBALL, {56: PAGE}))


def test_law_must_apply_to_the_incident():
    bad = dict(GOOD, law="Law 11")
    problems = laws_clerk.check_citation(bad, HANDBALL, {56: PAGE})
    assert any("does not apply" in p for p in problems)


def test_page_must_come_from_the_search():
    assert any("never returned" in p for p in laws_clerk.check_citation(GOOD, HANDBALL, {}))


def test_no_invented_numbers():
    bad = dict(GOOD, explanation="If the arm is more than 30 cm from the body it is handball.")
    assert any("numbers" in p for p in laws_clerk.check_citation(bad, HANDBALL, {56: PAGE}))


def test_model_failure_falls_back_to_template(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("Foundry did not answer")
    monkeypatch.setattr(laws_clerk, "cite_law", boom)
    claim = laws_clerk.law_claim(HANDBALL, LAW_LINE["handball"][1])
    assert claim["author"] == "template" and claim["text"] == LAW_LINE["handball"][1]


class FakeModel:
    """Plays the model: first asks for a search, then gives its answers one after the other."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        assert "officiating" not in json.dumps(kwargs["messages"][1])  # never shown the outcome
        assert "penalty_awarded" not in kwargs["messages"][1]["content"]
        if self.calls == 1:
            call = SimpleNamespace(id="call1", type="function", function=SimpleNamespace(
                name="search_laws", arguments=json.dumps({"query": "handles the ball", "law": "Law 12"})))
            msg = SimpleNamespace(content=None, tool_calls=[call],
                                  model_dump=lambda **k: {"role": "assistant", "content": None})
        else:
            msg = SimpleNamespace(content=json.dumps(self.answers.pop(0)), tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_whole_loop_and_card_verifier(monkeypatch):
    model = FakeModel(GOOD)
    monkeypatch.setattr(laws_clerk, "client", lambda: model)
    monkeypatch.setattr(laws_clerk, "search_laws", lambda q, law=None, top=3: [PAGE])
    claim = laws_clerk.law_claim(HANDBALL, LAW_LINE["handball"][1])
    assert claim["author"] == "laws_clerk"
    assert claim["text"].startswith("Law 12, Handling the ball:")

    settled = DATA.settle_time("INC-03")
    card = decided_card(DATA, "INC-03", {"period": settled[0], "t": settled[1] + 2.0})
    card["claims"] = [claim if c["id"] == "law" else c for c in card["claims"]]
    assert "law" in verify_card(card, DATA)["publishable_claims"]


def test_quote_cut_mid_sentence_is_rejected():
    bad = dict(GOOD, quote="It is an offence if a player:")
    assert any("middle of a sentence" in p for p in laws_clerk.check_citation(bad, HANDBALL, {56: PAGE}))


def test_explanation_must_be_short():
    bad = dict(GOOD, explanation=" ".join(["word"] * 40))
    assert any("explanation has 40 words" in p for p in laws_clerk.check_citation(bad, HANDBALL, {56: PAGE}))


def test_model_gets_one_chance_to_fix(monkeypatch):
    first = dict(GOOD, quote="It is an offence if a player:")  # rejected: stops mid-sentence
    model = FakeModel(first, GOOD)
    monkeypatch.setattr(laws_clerk, "client", lambda: model)
    monkeypatch.setattr(laws_clerk, "search_laws", lambda q, law=None, top=3: [PAGE])
    claim = laws_clerk.law_claim(HANDBALL, LAW_LINE["handball"][1])
    assert claim["author"] == "laws_clerk" and claim["fixes"] == 1


def test_still_wrong_after_the_fix_falls_back(monkeypatch):
    bad = dict(GOOD, law="Law 11")
    model = FakeModel(bad, bad)
    monkeypatch.setattr(laws_clerk, "client", lambda: model)
    monkeypatch.setattr(laws_clerk, "search_laws", lambda q, law=None, top=3: [PAGE])
    claim = laws_clerk.law_claim(HANDBALL, LAW_LINE["handball"][1])
    assert claim["author"] == "template"
