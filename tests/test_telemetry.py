"""Timing and tracing: every agent step is timed, every agent card says how long it took, and
tracing stays off (and harmless) without Application Insights. No Azure needed.

Run: py -m pytest tests
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import contradicteur, pipeline, telemetry  # noqa: E402
from test_agent_desk import DATA, agent_law_claim, keep_everything_model, no_narrator, run_with_agents  # noqa: E402


def test_span_records_time_even_when_the_step_fails():
    telemetry.RECORDS.clear()
    try:
        with telemetry.span("step", agent="laws_clerk", incident="INC-01"):
            raise ValueError("boom")
    except ValueError:
        pass
    rec = telemetry.RECORDS[-1]
    assert rec["name"] == "step" and rec["gen_ai.agent.name"] == "laws_clerk" and rec["seconds"] >= 0


def test_tracing_off_without_connection_string(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(telemetry, "_setup_done", False)
    monkeypatch.setattr(telemetry, "_tracer", None)
    assert telemetry.setup() is False


def test_agent_cards_carry_their_latency(monkeypatch):
    no_narrator(monkeypatch)
    monkeypatch.setattr(pipeline, "law_claim", agent_law_claim)
    monkeypatch.setattr(contradicteur, "client", keep_everything_model)
    telemetry.RECORDS.clear()
    results = [r for r in run_with_agents() if r["card"] is not None]
    assert results and all(r["latency_s"] >= 0 for r in results)
    agents = {r.get("gen_ai.agent.name") for r in telemetry.RECORDS}
    assert {"laws_clerk", "contradicteur"} <= agents
