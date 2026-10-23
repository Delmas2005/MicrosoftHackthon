"""Decision cards and the deterministic verifier.

A decision card is the only thing that ever reaches a screen. Agents write cards; this module
decides whether a card (or which of its claims) may be shown. It enforces the three rules we
promise the jury:

1. We never announce a decision before the officials do.
2. Every claim cites evidence: a recomputed measurement, an event in the feed, a Law, a player.
3. Every number in the text matches the evidence it cites.

The LLM Contradicteur agent calls `verify_card` as a tool and adds its own judgement on top
(does the sentence say what the evidence says?). This file is the part that cannot be argued with.

Card format (JSON):
{
  "incident_id": "INC-01",
  "phase": "checking" | "decided",
  "issued_at": {"period": 1, "t": 1460.0},
  "audience": "tv" | "stadium" | "fan" | "analyst" | "audio",
  "language": "en",
  "decision": null | {"value": "no_penalty", "source_event": "E00412"},
  "claims": [
    {"id": "c1", "kind": "fact" | "law" | "context" | "verdict", "text": "...",
     "evidence": [
        {"type": "measure", "path": "observers.REF.visible_share_at_key_moment", "value": 0.0},
        {"type": "event", "event_id": "E00410"},
        {"type": "law", "law": "Law 12"},
        {"type": "player", "id": "NBR-05"}
     ]}
  ]
}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tools.referee_eye import analyze_from_files

PHASES = {"checking", "decided"}
AUDIENCES = {"tv", "stadium", "fan", "analyst", "audio"}
KINDS = {"fact", "law", "context", "verdict"}
SETTLING_TYPES = {"var_check", "on_field_review", "decision", "card"}
NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?")


class MatchData:
    """Loads a generated match once and answers the verifier's questions."""

    def __init__(self, match_dir: str | Path):
        d = Path(match_dir)
        self.dir = d
        self.match = json.loads((d / "match.json").read_text())
        self.events = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]
        self.by_id = {e["event_id"]: e for e in self.events}
        self.players = {pid: p for t in self.match["teams"].values() for pid, p in {**t["starting"], **t["bench"]}.items()}
        self._incidents, self._analysis = {}, {}

    def incident(self, inc_id: str) -> dict:
        if inc_id not in self._incidents:
            self._incidents[inc_id] = json.loads((self.dir / "incidents" / f"{inc_id}.json").read_text())
        return self._incidents[inc_id]

    def analysis(self, inc_id: str) -> dict:
        if inc_id not in self._analysis:
            self._analysis[inc_id] = analyze_from_files(self.match, self.incident(inc_id))
        return self._analysis[inc_id]

    def settle_time(self, inc_id: str):
        """When the officials' final decision on this incident became public (period, t)."""
        inc = self.incident(inc_id)
        best = None
        for eid in inc["linked_events"]:
            e = self.by_id[eid]
            if e["type"] not in SETTLING_TYPES:
                continue
            t = e["t"] + (e.get("duration_s", 0.0) if e["type"] in ("var_check", "on_field_review") else 0.0)
            if best is None or (e["period"], t) > best:
                best = (e["period"], t)
        return best

    def key_moment(self, inc_id: str):
        inc = self.incident(inc_id)
        return (inc["period"], inc["t"] + inc["focal"]["dt"])


def _lookup(obj, path: str):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(0.011, 0.01 * abs(b))
    return a == b


def _number_forms(v) -> set[float]:
    out = set()
    if isinstance(v, bool):
        return out
    if isinstance(v, (int, float)):
        for x in (v, abs(v)):
            out |= {round(x, 2), round(x, 1), float(round(x))}
            if abs(x) <= 1.0:  # shares and probabilities are written as percentages
                out |= {float(round(x * 100)), round(x * 100, 1)}
            if abs(x) < 10:   # metres are often written in centimetres
                out |= {float(round(x * 100))}
    elif isinstance(v, (list, tuple)):
        for x in v:
            out |= _number_forms(x)
    elif isinstance(v, dict):
        for x in v.values():
            out |= _number_forms(x)
    return out


def verify_card(card: dict, data: MatchData) -> dict:
    errors: list[dict] = []

    def err(rule, detail, claim=None, fatal=False):
        errors.append({"rule": rule, "detail": detail, "claim": claim, "fatal": fatal})

    # ---------------------------------------------------------------- envelope
    for k in ("incident_id", "phase", "issued_at", "audience", "language", "claims"):
        if k not in card:
            err("schema", f"missing field '{k}'", fatal=True)
    if any(e["fatal"] for e in errors):
        return {"ok": False, "publishable_claims": [], "errors": errors}
    inc_id = card["incident_id"]
    try:
        data.incident(inc_id)
    except FileNotFoundError:
        err("schema", f"unknown incident {inc_id}", fatal=True)
        return {"ok": False, "publishable_claims": [], "errors": errors}
    if card["phase"] not in PHASES:
        err("schema", f"phase must be one of {sorted(PHASES)}", fatal=True)
    if card["audience"] not in AUDIENCES:
        err("schema", f"audience must be one of {sorted(AUDIENCES)}", fatal=True)

    # ---------------------------------------------------------------- rule 1: never ahead of the officials
    issued = (card["issued_at"]["period"], card["issued_at"]["t"])
    key = data.key_moment(inc_id)
    settled = data.settle_time(inc_id)
    if issued < key:
        err("timing", "card issued before the incident happened", fatal=True)
    if card["phase"] == "decided":
        dec = card.get("decision")
        if not dec:
            err("anticipation", "a 'decided' card needs a decision with its source event", fatal=True)
        else:
            src = data.by_id.get(dec.get("source_event", ""))
            if not src or src["event_id"] not in data.incident(inc_id)["linked_events"] or src["type"] not in SETTLING_TYPES:
                err("anticipation", f"decision source {dec.get('source_event')} is not an official decision on {inc_id}", fatal=True)
            if settled is None or issued < settled:
                err("anticipation", f"card issued at {issued} but the officials only settled this at {settled}", fatal=True)
            final = data.incident(inc_id)["officiating"]["final_decision"]
            if dec.get("value") != final:
                err("anticipation", f"decision '{dec.get('value')}' differs from the officials' '{final}'", fatal=True)
    else:
        if card.get("decision"):
            err("anticipation", "a 'checking' card must not carry a decision", fatal=True)
        if settled is not None and issued >= settled:
            err("timing", "decision already settled: issue a 'decided' card instead")

    if any(e["fatal"] for e in errors):
        return {"ok": False, "publishable_claims": [], "errors": errors}

    # ---------------------------------------------------------------- rules 2 and 3: evidence and numbers
    analysis = data.analysis(inc_id)
    inc = data.incident(inc_id)
    laws = [l.split(":")[0].strip() for l in inc["laws"]]
    publishable = []
    for c in card["claims"]:
        cid = c.get("id", "?")
        before = len(errors)
        if c.get("kind") not in KINDS:
            err("schema", f"kind must be one of {sorted(KINDS)}", cid)
        if c.get("kind") == "verdict" and card["phase"] != "decided":
            err("anticipation", "verdict claims are only allowed once the officials have decided", cid)
        ev = c.get("evidence") or []
        if not ev:
            err("evidence", "claim has no evidence", cid)
        allowed = set()
        for e in ev:
            t = e.get("type")
            if t == "measure":
                try:
                    truth = _lookup(analysis, e["path"])
                except (KeyError, IndexError, ValueError):
                    err("evidence", f"no measurement at '{e.get('path')}'", cid)
                    continue
                if not _same(e.get("value"), truth):
                    err("evidence", f"'{e['path']}' is {truth}, card says {e.get('value')}", cid)
                allowed |= _number_forms(truth)
            elif t == "event":
                src = data.by_id.get(e.get("event_id", ""))
                if not src:
                    err("evidence", f"unknown event {e.get('event_id')}", cid)
                    continue
                if src["event_id"] not in inc["linked_events"] and not (
                        src["period"] == inc["period"] and abs(src["t"] - inc["t"]) <= 90):
                    err("evidence", f"event {src['event_id']} is not part of {inc_id}", cid)
                allowed |= {float(x) for x in re.findall(r"\d+", src["clock"])}
                allowed |= _number_forms({k: v for k, v in src.items() if isinstance(v, (int, float)) and k not in ("period",)})
            elif t == "law":
                if e.get("law") not in laws:
                    err("evidence", f"{e.get('law')} is not a Law that applies to {inc_id} ({laws})", cid)
                allowed |= {float(x) for x in re.findall(r"\d+", e.get("law", ""))}
            elif t == "player":
                p = data.players.get(e.get("id", ""))
                if not p:
                    err("evidence", f"unknown player {e.get('id')}", cid)
                    continue
                allowed.add(float(p["number"]))
            else:
                err("evidence", f"unknown evidence type '{t}'", cid)
        for n in NUM_RE.findall(c.get("text", "")):
            x = float(n.replace(",", "."))
            if not any(abs(x - a) < 0.051 for a in allowed):
                err("number", f"'{n}' in the text is not backed by the cited evidence", cid)
        if len(errors) == before:
            publishable.append(cid)

    return {
        "ok": not errors,
        "publishable_claims": publishable,
        "rejected_claims": [c.get("id") for c in card["claims"] if c.get("id") not in publishable],
        "errors": errors,
    }
