"""Laws Clerk (le Juriste): finds and cites the clause of the Laws of the Game that applies.

How it works:
1. The model gets a neutral description of the incident (never the outcome) and one tool,
   `search_laws`, which searches the IFAB Laws of the Game in Azure AI Search.
2. It searches as many times as it needs (up to MAX_SEARCHES), reads the pages, and answers
   with the Law, the clause title, an exact quote and a plain-English explanation.
3. `check_citation` then verifies the answer without any model: the Law must apply to this
   incident, the quote must appear word for word on a page the search returned for that Law,
   and the explanation must be short and must not invent numbers.
4. If the check fails, the model gets the list of problems and one chance to fix its answer.
   If it still fails, the card falls back to the template sentence, so a wrong citation never
   reaches a screen.

Try it (from the repo root):
    py -m agents.laws_clerk INC-03          # one incident
    py -m agents.laws_clerk --all           # all five incidents of match_001
"""
from __future__ import annotations

import argparse
import json
import os
import re

from agents.llm import MODEL_STRONG, client, parse_json

INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "laws-of-the-game")
MAX_SEARCHES = 4
TOP_PAGES = 3
MAX_FIXES = 1               # how many times the model may correct an answer the check rejected
MAX_EXPLANATION_WORDS = 30  # it goes on a TV panel: keep it short

SYSTEM_PROMPT = """You are the Laws Clerk of a football broadcast. You explain which clause of the
IFAB Laws of the Game applies to a refereeing incident, for viewers.

Rules:
- Use the search_laws tool to read the Laws before answering. Search with the words the Laws
  use (for example "handles the ball", "denies an obvious goal-scoring opportunity", "holding").
- Only cite a Law from the list of Laws that apply to the incident.
- The quote is the sentence or bullet point that states the offence the officials are judging
  here: the test they apply. Not a definition (for example where the arm ends), not a general
  rule about free kicks. It must be a complete sentence or bullet point, never ending with ":",
  at most 40 words, copied word for word from a page that search_laws returned. Do not fix its
  spelling, do not join text from different places.
- The explanation says, in one sentence of at most 25 words that a fan understands, what makes
  this an offence. Never say what the decision on this incident was or should be: the officials
  decide that. Do not write any number except the Law number.

When you have found the clause, answer with this JSON object and nothing else:
{"law": "Law 12", "clause": "short title of the clause", "page": 56,
 "quote": "exact words from the Laws", "explanation": "plain-English explanation"}"""

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_laws",
        "description": "Search the IFAB Laws of the Game 2025/26. Returns the best matching pages.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to look for, in the language of the Laws."},
                "law": {"type": "string", "description": "Optional: only search this Law, e.g. 'Law 12'."},
            },
            "required": ["query"],
        },
    },
}


# --------------------------------------------------------------------------- the search tool

_search_client = None


def search_laws(query: str, law: str | None = None, top: int = TOP_PAGES) -> list[dict]:
    """Best matching pages of the Laws: [{"page", "section", "text"}]."""
    global _search_client
    if _search_client is None:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient

        _search_client = SearchClient(os.environ["AZURE_SEARCH_ENDPOINT"], INDEX_NAME,
                                      AzureKeyCredential(os.environ["AZURE_SEARCH_KEY"]))
    flt = None
    if law:
        if not re.fullmatch(r"Law \d{1,2}", law):  # only accept "Law N": nothing else goes into the filter
            return []
        flt = f"section eq '{law}'"
    hits = _search_client.search(query, filter=flt, top=top, select=["page", "section", "text"])
    return [{"page": h["page"], "section": h["section"], "text": h["text"]} for h in hits]


# --------------------------------------------------------------------------- the deterministic check

def _normalise(text: str) -> str:
    """Lower case, letters and digits only: PDF line breaks and odd quotes do not matter."""
    text = text.lower().replace("’", "'").replace("‘", "'")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def laws_that_apply(incident: dict) -> list[str]:
    """["Law 12"] from ["Law 12: handling the ball"]."""
    return [l.split(":")[0].strip() for l in incident["laws"]]


def check_citation(citation: dict, incident: dict, pages_seen: dict[int, dict]) -> list[str]:
    """Problems with a citation (an empty list means it can be shown)."""
    problems = []
    law = citation.get("law")
    if law not in laws_that_apply(incident):
        problems.append(f"{law} does not apply to {incident['incident_id']} ({laws_that_apply(incident)})")
    page = pages_seen.get(citation.get("page"))
    if page is None:
        problems.append(f"page {citation.get('page')} was never returned by search_laws")
    elif page["section"] != law:
        problems.append(f"page {page['page']} belongs to {page['section']}, not {law}")
    quote = _normalise(citation.get("quote", ""))
    words = len(quote.split())
    if words < 4 or words > 50:
        problems.append(f"quote has {words} words (4 to 50 expected)")
    elif page is not None and quote not in _normalise(page["text"]):
        problems.append("quote is not word for word on that page")
    if citation.get("quote", "").rstrip().endswith(":"):
        problems.append("quote stops in the middle of a sentence (ends with ':')")
    explanation = citation.get("explanation", "")
    n_words = len(explanation.split())
    if not explanation or n_words > MAX_EXPLANATION_WORDS:
        problems.append(f"explanation has {n_words} words (1 to {MAX_EXPLANATION_WORDS} expected)")
    if not citation.get("clause") or len(citation["clause"]) > 80:
        problems.append("clause title is missing or too long")
    # The card shows "Law 12, <clause>: <explanation>": the only number allowed is the Law's.
    law_number = (law or "").replace("Law", "").strip()
    stray = [n for n in re.findall(r"\d+", f"{citation.get('clause', '')} {explanation}") if n != law_number]
    if stray:
        problems.append(f"clause or explanation contains numbers that are not backed: {stray}")
    return problems


# --------------------------------------------------------------------------- the agent

def describe(incident: dict) -> str:
    """What the model is told: a neutral description, never the outcome."""
    off = incident.get("officiating", {})
    lines = [
        f"Incident {incident['incident_id']} at {incident.get('clock', '?')}: {incident['headline']}.",
        f"Type: {incident['type'].replace('_', ' ')}.",
        f"What the officials need to judge: {incident['focal']['what']}.",
        f"Decision on the field: {str(off.get('on_field_decision', 'unknown')).replace('_', ' ')}.",
    ]
    check = off.get("var_checking") or (off.get("var") or {}).get("check")
    if check:
        lines.append(f"VAR is checking: {check.replace('_', ' ')}.")
    lines.append("Laws that apply: " + "; ".join(incident["laws"]) + ".")
    return "\n".join(lines)


def cite_law(incident: dict, verbose: bool = False) -> dict:
    """Ask the model for the clause, check it, let it fix the answer once.
    Returns the citation plus 'problems', 'pages_seen' and 'fixes'."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": describe(incident)}]
    pages_seen: dict[int, dict] = {}
    searches = 0
    for fixes in range(MAX_FIXES + 1):
        citation, searches = _answer(messages, pages_seen, searches, verbose)
        citation["problems"] = check_citation(citation, incident, pages_seen) if citation else ["no JSON answer"]
        citation["fixes"] = fixes
        if not citation["problems"] or fixes == MAX_FIXES:
            break
        if verbose:
            print(f"  check failed, asking for a fix: {citation['problems']}")
        messages.append({"role": "user", "content": (
            "Your answer failed these checks:\n- " + "\n- ".join(citation["problems"]) +
            "\nFix them (search again if you need to) and answer with the JSON object again.")})
    citation["pages_seen"] = sorted(pages_seen)
    return citation


def _answer(messages: list, pages_seen: dict[int, dict], searches: int, verbose: bool):
    """Let the model search until it answers. Adds its answer to `messages`."""
    msg = None
    for _ in range(MAX_SEARCHES + 2):  # hard stop, whatever the model does
        # Once the search budget is spent, the tool is taken away so the model has to answer.
        tools = {"tools": [SEARCH_TOOL]} if searches < MAX_SEARCHES else {}
        reply = client().chat.completions.create(
            model=MODEL_STRONG,
            messages=messages,
            max_completion_tokens=6000,  # gpt-5-mini thinks before it answers: leave it room
            **tools,
        )
        msg = reply.choices[0].message
        if not msg.tool_calls:
            break
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            searches += 1
            pages = search_laws(args.get("query", ""), args.get("law")) if searches <= MAX_SEARCHES else []
            if verbose:
                print(f"  search {searches}: {args} -> pages {[p['page'] for p in pages]}")
            for p in pages:
                pages_seen[p["page"]] = p
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(pages)})
    messages.append({"role": "assistant", "content": msg.content or ""})
    try:
        citation = parse_json(msg.content)
    except (ValueError, TypeError):
        citation = {}
    return (citation if isinstance(citation, dict) else {}), searches


def law_claim(incident: dict, fallback_text: str, verbose: bool = False) -> dict:
    """The 'law' claim for a decision card: the agent's citation if it passes the check,
    otherwise the template sentence. Never raises: a broken model must not break the screen."""
    law = laws_that_apply(incident)[0]
    try:
        c = cite_law(incident, verbose)
    except Exception as exc:  # timeout, quota, network...
        c = {"problems": [f"agent failed: {exc}"]}
    if not c["problems"]:
        return {"id": "law", "kind": "law", "author": "laws_clerk", "fixes": c.get("fixes", 0),
                "text": f"{c['law']}, {c['clause']}: {c['explanation']}",
                "evidence": [{"type": "law", "law": c["law"], "page": c["page"], "quote": c["quote"]}]}
    return {"id": "law", "kind": "law", "author": "template", "fallback_reason": c["problems"],
            "text": fallback_text, "evidence": [{"type": "law", "law": law}]}


# --------------------------------------------------------------------------- try it

def main():
    from agents.card import MatchData, verify_card
    from agents.fallback_narrator import LAW_LINE, decided_card

    ap = argparse.ArgumentParser()
    ap.add_argument("incident", nargs="?", default="INC-03")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--match", default="data/match_001")
    args = ap.parse_args()
    data = MatchData(args.match)
    ids = [i["incident_id"] for i in data.match["incidents"]] if args.all else [args.incident]

    for inc_id in ids:
        inc = data.incident(inc_id)
        print(f"\n=== {inc_id}: {inc['headline']}")
        claim = law_claim(inc, LAW_LINE[inc["type"]][1], verbose=True)
        print(f"  author: {claim['author']}" + (f" (after {claim['fixes']} fix)" if claim.get("fixes") else ""))
        print(f"  text:   {claim['text']}")
        if claim["author"] == "laws_clerk":
            ev = claim["evidence"][0]
            print(f"  quote (page {ev['page']}): \"{ev['quote']}\"")
        else:
            print(f"  why the template was used: {claim['fallback_reason']}")

        # The claim must also pass the card verifier, like every claim that reaches a screen.
        settled = data.settle_time(inc_id)
        card = decided_card(data, inc_id, {"period": settled[0], "t": settled[1] + 2.0})
        card["claims"] = [claim if c["id"] == "law" else c for c in card["claims"]]
        v = verify_card(card, data)
        print(f"  verifier: {'OK' if 'law' in v['publishable_claims'] else 'REJECTED ' + str(v['errors'])}")


if __name__ == "__main__":
    main()
