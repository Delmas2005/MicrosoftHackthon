"""End-to-end live pipeline: replay -> spotter -> narrator -> verifier -> screens.

    python -m stream.run_live --speed 30                 # whole match, cards printed as they publish
    python -m stream.run_live --from 1:22:50 --until 1:25:00 --speed 5
    python -m stream.run_live --no-wait --out cards.jsonl

Today the narrator is the template narrator (agents/fallback_narrator.py). When the Foundry agents
are connected, they replace it; the template stays as the fallback if a model call fails or the
verifier rejects every claim.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agents.card import MatchData, verify_card
from agents.fallback_narrator import checking_card, decided_card
from stream.replay import live_events, load_events, paced, parse_at
from stream.spotter import Spotter


def publish(card: dict, data: MatchData) -> dict | None:
    v = verify_card(card, data)
    if not v["publishable_claims"]:
        return {"card": card, "verification": v, "published": False}
    shown = dict(card)
    shown["claims"] = [c for c in card["claims"] if c["id"] in v["publishable_claims"]]
    return {"card": shown, "verification": v, "published": True}


def render(result: dict) -> str:
    c = result["card"]
    head = f"[{c['issued_at']['period']}H {c['issued_at']['t']:7.1f}s] {c['incident_id']} {c['phase'].upper():8s} {c['audience']:7s}"
    if not result["published"]:
        return head + "  BLOCKED: " + "; ".join(e["detail"] for e in result["verification"]["errors"])
    lines = [head] + [f"    - {cl['text']}" for cl in c["claims"]]
    rej = result["verification"]["rejected_claims"]
    if rej:
        lines.append(f"    (dropped by verifier: {', '.join(rej)})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="data/match_001")
    ap.add_argument("--speed", type=float, default=30.0)
    ap.add_argument("--from", dest="start")
    ap.add_argument("--until")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--out", help="also write published cards to this JSONL file")
    args = ap.parse_args()
    data = MatchData(args.match)
    spotter = Spotter()
    out = open(args.out, "w") if args.out else None
    feed = paced(live_events(load_events(Path(args.match))), args.speed, parse_at(args.start), parse_at(args.until), not args.no_wait)
    for e in feed:
        for s in spotter.on_event(e):
            if s["signal"] == "var_check_started" and not s.get("review"):
                cards = [checking_card(data, s["incident_id"], s["at"], s["event_id"])]
            elif s["signal"] == "decision_settled":
                cards = [decided_card(data, s["incident_id"], s["at"], "stadium"),
                         decided_card(data, s["incident_id"], s["at"], "tv")]
            else:
                continue
            for card in cards:
                res = publish(card, data)
                print(render(res), flush=True)
                if out and res["published"]:
                    out.write(json.dumps(res["card"]) + "\n")
    if out:
        out.close()


if __name__ == "__main__":
    main()
