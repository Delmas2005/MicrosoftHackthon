"""Timing and tracing for the agents.

Every agent step runs inside `span(...)`. That does two things:
- it always records how long the step took (`RECORDS`), which `stream/latency.py` turns into the
  latency table in the README;
- if APPLICATIONINSIGHTS_CONNECTION_STRING is set in .env, it also sends an OpenTelemetry trace to
  Azure Application Insights, where the Microsoft Foundry project shows it under "Tracing": one
  trace per decision, with the Contradicteur and the Narrator inside it (the Laws Clerk has its own
  trace per incident), and every model call (model, tokens, time) under the agent that made it.

Without the connection string (tests, or no Azure), only the timings are kept. Nothing here can
stop a card: a tracing error is printed once and ignored.

Extra package for the traces (not needed otherwise):
    py -m pip install azure-monitor-opentelemetry
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

RECORDS: list[dict] = []          # {"name", "seconds", **attributes}, in the order steps finished
_lock = threading.Lock()
_tracer = None
_setup_done = False


def setup() -> bool:
    """Connect to Application Insights once, if a connection string is configured. True if tracing is on."""
    global _tracer, _setup_done
    if _setup_done:
        return _tracer is not None
    _setup_done = True
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import trace

        os.environ.setdefault("OTEL_SERVICE_NAME", "fourth-official")
        configure_azure_monitor(connection_string=conn)
        _tracer = trace.get_tracer("fourth_official")
        print("(tracing to Azure Application Insights is on)")
    except Exception as exc:  # never let tracing break the match
        print(f"(tracing off: {type(exc).__name__}: {exc})")
        _tracer = None
    return _tracer is not None


@contextmanager
def span(name: str, agent: str | None = None, **attributes):
    """Time a step (and trace it, if tracing is on). `agent` marks the step as an agent run."""
    attrs = {k: v for k, v in attributes.items() if isinstance(v, (str, int, float, bool))}
    if agent:
        attrs.update({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent})
    start = time.perf_counter()
    try:
        if _tracer is not None:
            with _tracer.start_as_current_span(name, attributes=attrs) as s:
                yield s
        else:
            yield None
    finally:
        seconds = time.perf_counter() - start
        with _lock:
            RECORDS.append({"name": name, "seconds": round(seconds, 3), **attrs})


def traced_create(create):
    """Wrap `client.chat.completions.create`: one span per model call, with the model and the
    tokens used (OpenTelemetry GenAI attribute names, which Foundry's tracing view reads)."""
    def wrapper(*args, **kwargs):
        model = kwargs.get("model", "")
        with span(f"chat {model}", **{"gen_ai.operation.name": "chat", "gen_ai.system": "az.ai.openai",
                                       "gen_ai.request.model": model}) as s:
            response = create(*args, **kwargs)
            usage = getattr(response, "usage", None)
            if s is not None and usage is not None:
                s.set_attribute("gen_ai.usage.input_tokens", getattr(usage, "prompt_tokens", 0) or 0)
                s.set_attribute("gen_ai.usage.output_tokens", getattr(usage, "completion_tokens", 0) or 0)
                s.set_attribute("gen_ai.response.model", getattr(response, "model", "") or "")
            return response
    return wrapper


def flush() -> None:
    """Send the traces still waiting (call at the end of a script)."""
    if _tracer is None:
        return
    try:
        from opentelemetry import trace
        trace.get_tracer_provider().force_flush()
    except Exception:
        pass
