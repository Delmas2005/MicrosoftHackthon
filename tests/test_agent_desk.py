"""The live pipeline with agents: every improved card still passes the verifier, comes after the
officials, and a failing agent never takes a card off the screen. Models are faked: no Azure needed.

Run: py -m pytest tests
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import contradicteur, pipeline  # noqa: E402
from agents.card import MatchData, verify_card  # noqa: E402
from stream.replay import live_events, load_events  # noqa: E402
from stream.spotter import Spotter  # noqa: E402

DATA = MatchData(ROOT / "data" / "match_001")
INCIDENTS = {i["incident_id"] for i in DATA.match["incidents"]}


def agent_law_claim(incident, fallback_text, verbose=False):
    law = incident["laws"][0].split(":")[0]
    return {"id": "law", "kind": "law", "author": "laws_clerk", "fixes": 0,
            "text": f"{law}, Test clause: The officials apply the rule.",
            "evidence": [{"type": "law", "law": law, "page": 1, "quote": "a quote"}]}


def keep_everything_model():
    def create(**kwargs):
        ids = [c["id"] for c in json.loads(kwargs["messages"][1]["content"])["claims"]]
        answer = {"claims": [{"id": i, "decision": "keep"} for i in ids], "referees_call": False}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def no_narrator(monkeypatch):
    monkeypatch.setattr(pipeline, "NARRATOR_AUDIENCES", ())


def run_with_agents():
    desk, results = pipeline.AgentDesk(DATA), []
    sp = Spotter()
    for e in live_events(load_events(ROOT / "data" / "match_001")):
        for s in sp.on_event(e):
            if s["signal"] == "incident_detected":
                desk.start_incident(s["incident_id"])
            elif s["signal"] == "var_check_started" and not s.get("review"):
                desk.checking(s["incident_id"], s["at"], s["event_id"])
            elif s["signal"] == "decision_settled":
                desk.decided(s["incident_id"], s["at"], ("tv", "stadium"))
        results += desk.ready()
    return results + desk.drain()


def test_agent_cards_for_every_incident(monkeypatch):
    no_narrator(monkeypatch)
    monkeypatch.setattr(pipeline, "law_claim", agent_law_claim)
    monkeypatch.setattr(contradicteur, "client", keep_everything_model)
    results = run_with_agents()
    cards = [r["card"] for r in results if r["published"]]
    assert {c["incident_id"] for c in cards if c["phase"] == "checking"} == INCIDENTS
    assert {c["incident_id"] for c in cards if c["phase"] == "decided"} == INCIDENTS
    for c in cards:
        assert c["revision"] == 2 and c["author"] == "agents"
        assert verify_card(c, DATA)["ok"], (c["incident_id"], c["phase"])
        assert any(cl.get("author") == "laws_clerk" for cl in c["claims"])
        if c["phase"] == "decided":
            assert (c["issued_at"]["period"], c["issued_at"]["t"]) >= DATA.settle_time(c["incident_id"])


def test_agents_down_still_gives_safe_cards(monkeypatch):
    def down(*a, **k):
        raise ConnectionError("Foundry unreachable")
    from agents import narrator
    monkeypatch.setattr(narrator, "client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=down))))
    monkeypatch.setattr(pipeline, "law_claim", lambda inc, text, verbose=False: {
        "id": "law", "kind": "law", "author": "template", "text": text,
        "evidence": [{"type": "law", "law": inc["laws"][0].split(":")[0]}]})
    monkeypatch.setattr(contradicteur, "client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=down))))
    results = run_with_agents()
    shown = [r for r in results if r["published"]]
    assert len(shown) == 15  # 5 checking + 10 decided: the Narrator's cards are simply not shown
    assert all("Narrator" in r["error"] for r in results if not r["published"])
    for r in shown:
        assert verify_card(r["card"], DATA)["ok"]
        assert r["review"]["status"].startswith("unavailable")


def test_referees_call_is_the_same_on_every_screen(monkeypatch):
    # The fake reviewer flags the TV card only: the stadium card must follow the TV card.
    def create(**kwargs):
        body = json.loads(kwargs["messages"][1]["content"])
        ids = [c["id"] for c in body["claims"]]
        flag = body["context"]["audience"] == "tv" and body["context"]["phase"] == "decided"
        answer = {"claims": [{"id": i, "decision": "keep"} for i in ids], "referees_call": flag}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))])
    no_narrator(monkeypatch)
    monkeypatch.setattr(pipeline, "law_claim", agent_law_claim)
    monkeypatch.setattr(contradicteur, "client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    decided = [r["card"] for r in run_with_agents() if r["card"]["phase"] == "decided"]
    assert len(decided) == 10
    assert all(c.get("framing") == "referees_call" for c in decided)


def test_narrated_cards_join_the_live_pipeline(monkeypatch):
    from agents import narrator

    def create(**kwargs):  # a narrator that always writes "<decision>" from the verdict claim
        body = json.loads(kwargs["messages"][1]["content"])
        verdict = next(c for c in body["claims"] if c["id"] == "verdict")
        answer = {"claims": [{"text": verdict["text"], "sources": ["verdict"]}]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))])
    monkeypatch.setattr(pipeline, "law_claim", agent_law_claim)
    monkeypatch.setattr(contradicteur, "client", keep_everything_model)
    monkeypatch.setattr(narrator, "client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    narrated = [r["card"] for r in run_with_agents() if r["published"] and r["card"]["revision"] == 3]
    assert len(narrated) == 15  # stadium, fan and audio for each of the 5 incidents
    assert {c["audience"] for c in narrated} == {"stadium", "fan", "audio"}
    for c in narrated:
        assert verify_card(c, DATA)["ok"]
