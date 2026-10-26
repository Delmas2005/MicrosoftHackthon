"""Narrator: rewrites a verified card for each screen, audience, club and language.

It only ever works from claims that have already passed the verifier and the Contradicteur.
Every sentence it writes must name the claims it comes from (`sources`), and inherits their
evidence. So the narrated card goes through the same checks as any other card:
1. a check without any model: sources exist, length fits the screen, numbers written as digits
   (a number spelled out in words could not be checked against the evidence);
2. the card verifier (every number in the new text must match the evidence of its sources);
3. the Contradicteur (an empathetic sentence for an unhappy fan must not turn into blame).
If any step fails, the screen keeps the card it already had. Nothing unverified is ever shown.

Audiences:
    stadium  one short line for the big screen, in English
    fan      the "Why?" view on a supporter's phone: their club, their language, honest and kind
    audio    a spoken version (read aloud by the phone), in English

Try it (from the repo root):
    py -m agents.narrator INC-01                  # stadium, fan (Spanish) and audio for INC-01
    py -m agents.narrator --all --language fr     # every incident, fan card in French
"""
from __future__ import annotations

import argparse
import json
import re

from agents.card import MatchData
from agents.contradicteur import review_card
from agents.llm import MODEL_FAST, client, parse_json

AUDIENCES = {
    "stadium": {
        "max_claims": 2, "max_words": 16, "language_fixed": "en",
        "brief": "The big screen in the stadium. One or two very short sentences, 16 words in total at "
                 "most, readable from the stands. Start with the decision. No player names.",
    },
    "fan": {
        "max_claims": 3, "max_words": 60, "language_fixed": None,
        "brief": "The 'Why?' view on the phone of a supporter of {club}, who may be unhappy with this "
                 "decision. Write in {language_name}. Up to three short sentences: the decision, the "
                 "rule, and what the referee could see. Be honest and kind: acknowledge it is hard to "
                 "take, but never suggest the decision was wrong and never take sides.",
    },
    "audio": {
        "max_claims": 3, "max_words": 70, "language_fixed": "en",
        "brief": "A spoken version, read aloud by a phone for someone who cannot look at the screen. "
                 "Natural spoken English: three separate sentences (the decision, the rule, what the "
                 "referee could see), 60 words in total. Write units in full (metres, per cent) but keep "
                 "the numbers as digits.",
    },
}

LANGUAGE_NAMES = {"en": "English", "fr": "French", "es": "Spanish", "pt": "Portuguese", "de": "German",
                  "it": "Italian", "nl": "Dutch", "ar": "Arabic"}

# Numbers written in words cannot be checked against the evidence: the Narrator must use digits.
NUMBER_WORDS = {
    "en": "zero two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
          "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred",
    "fr": "zéro un deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze quinze seize "
          "vingt trente quarante cinquante soixante cent",
    "es": "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince "
          "dieciséis veinte treinta cuarenta cincuenta sesenta setenta ochenta noventa cien ciento",
    "pt": "zero um dois três quatro cinco seis sete oito nove dez onze doze vinte trinta quarenta cinquenta cem",
    "de": "null eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf zwanzig dreißig vierzig fünfzig hundert",
    "it": "zero uno due tre quattro cinque sei sette otto nove dieci undici dodici venti trenta quaranta cinquanta cento",
    "nl": "nul een twee drie vier vijf zes zeven acht negen tien elf twaalf twintig dertig veertig vijftig honderd",
}
# Words that are also ordinary words in some language ("un" in French, "een" in Dutch...) are not
# numbers in practice when they stand alone in a sentence: only check the unambiguous ones there.
AMBIGUOUS = {"fr": {"un", "neuf"}, "es": {"uno"}, "pt": {"um"}, "nl": {"een"}, "it": {"uno"}}

KIND_PRIORITY = ["verdict", "fact", "law", "context"]

SYSTEM_PROMPT = """You are the Narrator of a football broadcast. You rewrite a verified decision card
for one screen. You get the card's claims (each with an id) and a brief for the screen.

Rules:
- Only say what the claims say. Do not add facts, opinions, numbers or names that are not in them.
- Each sentence you write lists the ids of the claims it comes from in "sources". You may merge
  claims or leave some out, but never write a sentence without a source.
- Keep every number exactly as it appears in its claim, written as digits (20 m, 0%, 9 cm): write
  "0%" or "0 per cent", not "none" or "nothing"; "100%", not "fully".
- Keep the rule and the facts in separate sentences. Never join them into a conclusion of your own
  ("the arm was away from his body, making it unnaturally bigger", "so it was handball"): only the
  officials' decision says how the rule applied.
- Never say or hint that the officials were wrong, lucky, harsh or blind, and never take sides.
- If the card is a "referee's call", you may say it was a judgement call for the referee.

Answer with this JSON object and nothing else:
{"claims": [{"text": "the sentence", "sources": ["verdict", "law"]}]}"""


# --------------------------------------------------------------------------- who loses out

FAVOURS_DEFENCE = {"no_penalty", "goal_disallowed_offside"}


def aggrieved_club(data: MatchData, inc_id: str) -> str:
    """The team the final decision went against: its supporters are the ones asking 'why?'."""
    inc = data.incident(inc_id)
    final = inc["officiating"]["final_decision"]
    code = inc["attacking_team"] if final in FAVOURS_DEFENCE else inc["defending_team"]
    return data.match["teams"][code]["name"]


# --------------------------------------------------------------------------- the deterministic check

def _number_words(text: str, language: str) -> list[str]:
    words = set(NUMBER_WORDS.get(language, "").split()) - AMBIGUOUS.get(language, set())
    if language != "en":
        words |= set(NUMBER_WORDS["en"].split())  # English number words slip into other languages
    return [w for w in re.findall(r"[^\W\d_]+", text.lower()) if w in words]


def check_narration(answer: dict, source: dict, audience: str, language: str) -> list[str]:
    """Problems with the Narrator's answer (an empty list means it can go to the verifier)."""
    spec = AUDIENCES[audience]
    problems = []
    claims = answer.get("claims")
    if not isinstance(claims, list) or not claims:
        return ["no sentences"]
    if len(claims) > spec["max_claims"]:
        problems.append(f"{len(claims)} sentences, {spec['max_claims']} at most")
    known = {c["id"] for c in source["claims"]}
    total_words = 0
    for i, c in enumerate(claims, 1):
        text = (c.get("text") or "").strip() if isinstance(c, dict) else ""
        sources = c.get("sources") if isinstance(c, dict) else None
        if not text:
            problems.append(f"sentence {i} is empty")
            continue
        if not isinstance(sources, list) or not sources or any(s not in known for s in sources):
            problems.append(f"sentence {i} has missing or unknown sources {sources}")
        spelled = _number_words(text, language)
        if spelled:
            problems.append(f"sentence {i} writes numbers in words {spelled}: use digits")
        total_words += len(text.split())
    if total_words > spec["max_words"]:
        problems.append(f"{total_words} words, {spec['max_words']} at most")
    return problems


def build_card(answer: dict, source: dict, audience: str, language: str) -> dict:
    """The narrated card: each sentence carries the evidence of the claims it comes from."""
    by_id = {c["id"]: c for c in source["claims"]}
    claims = []
    for i, c in enumerate(answer["claims"], 1):
        srcs = [by_id[s] for s in c["sources"]]
        evidence, seen = [], set()
        for s in srcs:
            for e in s["evidence"]:
                key = json.dumps(e, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    evidence.append(e)
        kind = min((s["kind"] for s in srcs), key=KIND_PRIORITY.index)
        claims.append({"id": f"n{i}", "kind": kind, "text": c["text"].strip(),
                       "sources": c["sources"], "evidence": evidence})
    card = {k: v for k, v in source.items() if k not in ("claims", "audience", "language", "author", "revision")}
    card.update({"audience": audience, "language": language, "claims": claims,
                 "author": "narrator", "revision": 3, "narrated_from": source.get("audience")})
    return card


# --------------------------------------------------------------------------- the agent

def narrate(source: dict, data: MatchData, audience: str, language: str = "en", club: str | None = None) -> dict:
    """Ask the model for one screen's version of `source`, check it, review it.

    Returns {"card", "published", "problems", "verification", "review"}. `published` is False when
    anything failed: the screen then keeps the card it already shows."""
    spec = AUDIENCES[audience]
    language = spec["language_fixed"] or language
    club = club or aggrieved_club(data, source["incident_id"])
    brief = spec["brief"].format(club=club, language_name=LANGUAGE_NAMES.get(language, language))
    inc = data.incident(source["incident_id"])
    payload = {
        "screen": audience, "brief": brief, "phase": source["phase"],
        "referees_call": source.get("framing") == "referees_call",
        "incident": inc["headline"],
        "claims": [{"id": c["id"], "kind": c["kind"], "text": c["text"]} for c in source["claims"]],
    }
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    problems: list[str] = []
    for attempt in range(2):  # one chance to fix what the check found
        reply = client().chat.completions.create(model=MODEL_FAST, messages=messages,
                                                 temperature=0.3, max_tokens=600)
        content = reply.choices[0].message.content
        try:
            answer = parse_json(content)
        except (ValueError, TypeError):
            answer = {}
        problems = check_narration(answer, source, audience, language)
        if not problems:
            break
        messages += [{"role": "assistant", "content": content or ""},
                     {"role": "user", "content": "Your answer failed these checks:\n- " + "\n- ".join(problems)
                      + "\nFix them and answer with the JSON object again."}]
    if problems:
        return {"card": None, "published": False, "problems": problems}

    card = build_card(answer, source, audience, language)
    if audience == "fan":
        card["club"] = club
    result = review_card(card, data)
    # A sentence the verifier or the Contradicteur removed would leave a hole in a short text:
    # the narrated card is shown whole or not at all.
    complete = len(result["card"]["claims"]) == len(card["claims"])
    result["published"] = complete
    result["problems"] = [] if complete else [
        f"removed by the verifier or the Contradicteur: {result['verification']['rejected_claims']}"
        f" {[d['id'] + ': ' + d['reason'] for d in result['review']['dropped']]}"]
    # The "referee's call" badge belongs to the incident: every screen shows the TV card's.
    result["card"].pop("framing", None)
    if source.get("framing"):
        result["card"]["framing"] = source["framing"]
    result["review"]["referees_call"] = source.get("framing") == "referees_call"
    return result


def narrate_all(source: dict, data: MatchData, language: str = "es") -> list[dict]:
    """Stadium, fan and audio versions of one decided TV card. Never raises."""
    out = []
    for audience in AUDIENCES:
        try:
            out.append(narrate(source, data, audience, language))
        except Exception as exc:  # the screen keeps what it already shows
            out.append({"card": None, "published": False, "problems": [f"{type(exc).__name__}: {exc}"]})
        out[-1]["audience"] = audience
    return out


# --------------------------------------------------------------------------- try it

def main():
    from agents.fallback_narrator import decided_card

    ap = argparse.ArgumentParser()
    ap.add_argument("incident", nargs="?", default="INC-01")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--language", default="es", help="language of the fan card (en, fr, es, pt, de, it, nl)")
    ap.add_argument("--match", default="data/match_001")
    args = ap.parse_args()
    data = MatchData(args.match)
    ids = [i["incident_id"] for i in data.match["incidents"]] if args.all else [args.incident]
    for inc_id in ids:
        settled = data.settle_time(inc_id)
        source = decided_card(data, inc_id, {"period": settled[0], "t": settled[1] + 2.0}, "tv")
        print(f"\n=== {inc_id}: {data.incident(inc_id)['headline']}")
        for r in narrate_all(source, data, args.language):
            card = r["card"]
            label = r["audience"] + (f" ({card['language']}, {card.get('club')})" if card and r["audience"] == "fan" else "")
            if not r["published"]:
                print(f"  {label}: NOT SHOWN, {r['problems']}")
                continue
            print(f"  {label}:")
            for c in card["claims"]:
                print(f"    - {c['text']}   [from {', '.join(c['sources'])}]")


if __name__ == "__main__":
    main()
