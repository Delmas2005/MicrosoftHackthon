"""Shared connection to the models on Microsoft Foundry.

Every agent imports `client`, `MODEL_FAST` and `MODEL_STRONG` from here, so the keys and
deployment names live in one place: the .env file at the root of the repo (never committed).
"""
from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL_FAST = os.environ.get("MODEL_FAST", "gpt-4.1-mini")      # quick answers: Spotter, Narrator
MODEL_STRONG = os.environ.get("MODEL_STRONG", "gpt-5-mini")    # reasons first: Analyst, Laws Clerk, Contradicteur

_client: OpenAI | None = None


def client() -> OpenAI:
    """The Foundry client, created on first use (so tests can run without a .env file)."""
    global _client
    if _client is None:
        from agents import telemetry
        telemetry.setup()   # traces to Application Insights, if configured in .env
        _client = OpenAI(
            base_url=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            timeout=60,       # a live screen cannot wait forever: give up and use the template card
            max_retries=4,    # rate limits (429): the SDK waits as Azure asks, then tries again
        )
        # One trace span per model call (model, tokens), and a timing for stream/latency.py.
        _client.chat.completions.create = telemetry.traced_create(_client.chat.completions.create)
    return _client


def parse_json(text: str) -> dict:
    """Read the JSON object a model returned, even if it wrapped it in ```json fences."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))
