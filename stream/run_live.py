"""End-to-end live pipeline: replay -> spotter -> narrator -> verifier -> screens.

    python -m stream.run_live --speed 30                 # whole match, cards printed as they publish
    python -m stream.run_live --from 1:22:50 --until 1:25:00 --speed 5
    python -m stream.run_live --no-wait --out cards.jsonl
    python -m stream.run_live --agents --no-wait --out examples/cards_agents_match_001.jsonl
    python -m stream.run_live --agents --publish --speed 5 --from 1:22:30   # live to the screens (stream/pubsub.py)

Without --agents, every card comes from the template narrator (agents/fallback_narrator.py):
instant, free, no model. With --agents, the template card is still published at once
(revision 1), and the agents on Foundry improve it in the background (revision 2: the Laws Clerk's
citation, then the Contradicteur's review). Screens show the highest revision of each card.
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
    if c is None:
        return f"  (kept the card already on screen: {result.get('error')})"
    rev = c.get("revision", 1)
    who = {1: "TEMPLATE", 2: "AGENTS  ", 3: "NARRATOR"}.get(rev, "AGENTS  ")
    if c.get("audience") == "fan":
        who += f" {c.get('language')}, {c.get('club')}"
    head = (f"[{c['issued_at']['period']}H {c['issued_at']['t']:7.1f}s] {c['incident_id']} "
            f"{c['phase'].upper():8s} {c['audience']:7s} rev {rev} {who}")
    if c.get("framing") == "referees_call":
        head += "  [REFEREE'S CALL]"
    if "latency_s" in result:
        head += f"  (+{result['latency_s']:.1f} s)"
    if not result["published"]:
        return head + "  BLOCKED: " + "; ".join(e["detail"] for e in result["verification"]["errors"])
    lines = [head] + [f"    - {cl['text']}" for cl in c["claims"]]
    rej = result["verification"]["rejected_claims"]
    if rej:
        lines.append(f"    (dropped by verifier: {', '.join(rej)})")
    for d in result.get("review", {}).get("dropped", []):
        lines.append(f"    (dropped by Contradicteur: {d['id']}, {d['reason']})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="data/match_001")
    ap.add_argument("--speed", type=float, default=30.0)
    ap.add_argument("--from", dest="start")
    ap.add_argument("--until")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--out", help="also write published cards to this JSONL file")
    ap.add_argument("--agents", action="store_true", help="improve each card with the Foundry agents")
    ap.add_argument("--fan-language", default="es", help="language of the fan's 'Why?' card (with --agents)")
    ap.add_argument("--publish", action="store_true", help="push every card to the screens through Azure Web PubSub")
    args = ap.parse_args()
    data = MatchData(args.match)
    spotter = Spotter()
    out = open(args.out, "w", encoding="utf-8") if args.out else None
    desk = None
    if args.agents:
        from agents.pipeline import AgentDesk
        desk = AgentDesk(data, fan_language=args.fan_language)

    publisher = None
    if args.publish:
        from stream.pubsub import Publisher
        publisher = Publisher()

    def show(res):
        print(render(res), flush=True)
        if res["published"]:
            if out:
                out.write(json.dumps(res["card"], ensure_ascii=False) + "\n")
                out.flush()
            if publisher:
                publisher.send(res["card"])

    feed = paced(live_events(load_events(Path(args.match))), args.speed, parse_at(args.start), parse_at(args.until), not args.no_wait)
    for e in feed:
        for s in spotter.on_event(e):
            inc, at = s["incident_id"], s["at"]
            if s["signal"] == "incident_detected":
                if desk:
                    desk.start_incident(inc)
                continue
            if s["signal"] == "var_check_started" and not s.get("review"):
                cards = [checking_card(data, inc, at, s["event_id"])]
                if desk:
                    desk.checking(inc, at, s["event_id"])
            elif s["signal"] == "decision_settled":
                cards = [decided_card(data, inc, at, "stadium"), decided_card(data, inc, at, "tv")]
                if desk:
                    desk.decided(inc, at, ("tv", "stadium"))
            else:
                continue
            for card in cards:
                card["revision"] = 1
                show(publish(card, data))
        if desk:
            for res in desk.ready():
                show(res)
    if desk:
        print("\nEnd of the match: waiting for the agents to finish...", flush=True)
        for res in desk.drain():
            show(res)
    if out:
        out.close()
    if publisher:
        print(f"Web PubSub: {publisher.sent} cards sent, {publisher.failed} failed.")
    if desk:
        from agents import telemetry
        telemetry.flush()


if __name__ == "__main__":
    main()
