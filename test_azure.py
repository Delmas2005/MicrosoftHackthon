"""Check that both Foundry deployments answer.

Usage:
    pip install openai python-dotenv
    python test_azure.py
"""
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(
    base_url=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
)

for model in (os.environ["MODEL_FAST"], os.environ["MODEL_STRONG"]):
    try:
        reply = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: Fourth Official is live."}],
            max_completion_tokens=2000,  # reasoning models need room to think before answering
        )
        print(f"OK   {model}: {reply.choices[0].message.content.strip()}")
    except Exception as exc:  # show the error but keep testing the other model
        print(f"FAIL {model}: {exc}")
