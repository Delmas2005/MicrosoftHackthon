"""The RefBench agent runner, with a fake model: no Azure needed.

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
from refbench import agent  # noqa: E402
from refbench.evaluate import DATA  # noqa: E402

ITEMS = [json.loads(l) for l in (DATA / "items.jsonl").read_text().splitlines()]


def fake(answer):
    seen = []

    def create(**kwargs):
        seen.append(kwargs["messages"][1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), seen


def setup(monkeypatch, answer):
    model, seen = fake(answer)
    monkeypatch.setattr(agent, "client", lambda: model)
    monkeypatch.setattr(laws_clerk, "search_laws", lambda q, law=None, top=3: [
        {"page": 56, "section": law or "VAR protocol", "text": "Laws text"}])
    agent._law_cache.clear()
    return seen


def test_the_agent_never_sees_the_answer(monkeypatch):
    seen = setup(monkeypatch, {"decision": "no_penalty", "should_var_intervene": False})
    item = next(i for i in ITEMS if i["family"] == "penalty_contact")
    agent.judge(item)
    answer = json.loads(next(l for l in (DATA / "answers.jsonl").read_text().splitlines()
                             if json.loads(l)["item_id"] == item["item_id"]))
    for secret in ("correct_decision", "final_decision_after_var", "key_factors"):
        assert secret not in seen[0]
    assert ", ".join(answer["key_factors"]) not in seen[0]


def test_no_intervention_when_the_agent_agrees_with_the_field(monkeypatch):
    item = next(i for i in ITEMS if i["family"] == "handball")
    setup(monkeypatch, {"decision": item["on_field_decision"], "should_var_intervene": True})
    assert agent.judge(item)["should_var_intervene"] is False


def test_offside_is_factual(monkeypatch):
    item = next(i for i in ITEMS if i["family"] == "offside")
    other = "offside" if item["on_field_decision"] == "no_offside" else "no_offside"
    setup(monkeypatch, {"decision": other, "should_var_intervene": False})
    assert agent.judge(item)["should_var_intervene"] is True


def test_an_answer_outside_the_allowed_decisions_keeps_the_field_call(monkeypatch):
    item = next(i for i in ITEMS if i["family"] == "dogso")
    setup(monkeypatch, {"decision": "penalty", "should_var_intervene": True})
    r = agent.judge(item)
    assert r["decision"] == item["on_field_decision"] and not r["answered"] and not r["should_var_intervene"]


def test_subset_is_balanced():
    sub = agent.pick(ITEMS, 20)
    assert len(sub) == 100
    assert {f: sum(i["family"] == f for i in sub) for f in agent.DECISIONS} == {f: 20 for f in agent.DECISIONS}


def test_facts_are_written_out_in_words():
    behind = next(i for i in ITEMS if i["family"] == "offside" and i["facts"]["margin_m"] < 0)
    words = " ".join(agent.facts_in_words(behind))
    assert "behind the second-last defender" in words and "margin_m" not in words
    for it in ITEMS:   # every kind of incident has a written description
        assert all(isinstance(x, str) and x for x in agent.facts_in_words(it))


def test_a_borderline_call_is_left_to_the_referee(monkeypatch):
    item = next(i for i in ITEMS if i["family"] == "holding" and i["on_field_decision"] == "no_penalty")
    setup(monkeypatch, {"decision": "penalty", "should_var_intervene": True, "borderline": True})
    assert agent.judge(item)["should_var_intervene"] is False
