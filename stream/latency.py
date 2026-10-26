"""How long the agents take: measured on the 5 incidents of the match, against the real models.

    py -m stream.latency                   # 3 runs of each incident, about 10 to 15 minutes
    py -m stream.latency --repeats 1       # quick check

For each incident it does what the live pipeline does when the feed says so:
1. "VAR check started": the template card at once, then the agents' version of the TV panel.
   The Laws Clerk starts at that moment too (in the match it starts a few seconds earlier, when the
   incident is detected), so this is the slow case.
2. "decision settled": the TV card and the stadium board reviewed by the Contradicteur, then the
   Narrator's stadium line, fan card and audio version.

Times are measured from the signal to the moment the card is ready to be pushed, queueing
included, with 4 model calls at a time (the live setting). The table is printed in Markdown for the
README and saved to docs/latency.json.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from agents import telemetry
from agents.card import MatchData
from agents.fallback_narrator import decided_card
from agents.pipeline import AgentDesk
from stream.replay import live_events, load_events
from stream.run_live import publish
from stream.spotter import Spotter

STEPS = [  # (phase, audience, revision) -> what it is
    (("template", "tv", 1), "Template card, no model (revision 1)"),
    (("checking", "tv", 2), "TV panel during the VAR check: Laws Clerk, then Contradicteur"),
    (("decided", "tv", 2), "TV card once decided: Contradicteur"),
    (("decided", "stadium", 2), "Stadium board once decided: Contradicteur"),
    (("decided", "stadium", 3), "Stadium line: Narrator (then verifier and Contradicteur)"),
    (("decided", "fan", 3), "Fan's \"Why?\" card, in Spanish: Narrator"),
    (("decided", "audio", 3), "Audio version: Narrator"),
]


def signals(match: Path) -> dict[str, dict]:
    """The first VAR check and the decision of each incident, as the Spotter sees them."""
    spotter, out = Spotter(), defaultdict(dict)
    for e in live_events(load_events(match)):
        for s in spotter.on_event(e):
            if s["signal"] == "var_check_started" and not s.get("review"):
                out[s["incident_id"]].setdefault("checking", s)
            elif s["signal"] == "decision_settled":
                out[s["incident_id"]]["decided"] = s
    return out


def key(result: dict, phase: str) -> tuple:
    c = result.get("card")
    if c is None:  # a Narrator card that was not shown
        return (phase, result.get("audience", "?"), 3)
    return (phase, c["audience"], c.get("revision", 2))


def reason(result: dict) -> str:
    """Why a card was not shown (the template card stayed on screen), in one short line."""
    if result.get("error"):
        text = result["error"]
        for k in ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError"):
            if k in text:
                return f"{result.get('audience', '')}: model call failed ({k})"
        return f"{result.get('audience', '')}: {text[:150]}"
    return f"{result['card']['audience']}: every sentence was removed by the checks"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="data/match_001")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--fan-language", default="es")
    ap.add_argument("--out", default="docs/latency.json")
    args = ap.parse_args()

    data = MatchData(args.match)
    sig = signals(Path(args.match))
    times, shown, why = defaultdict(list), defaultdict(lambda: [0, 0]), defaultdict(int)

    def note(k, seconds, published):
        times[k].append(seconds)
        shown[k][1] += 1
        shown[k][0] += bool(published)

    for run in range(1, args.repeats + 1):
        for inc_id, s in sig.items():
            started = time.perf_counter()
            desk = AgentDesk(data, fan_language=args.fan_language)
            if "checking" in s:
                t = time.perf_counter()
                card = decided_card(data, inc_id, s["decided"]["at"], "tv")  # the slower template card
                note(("template", "tv", 1), round(time.perf_counter() - t, 3), publish(card, data)["published"])
                c = s["checking"]
                desk.checking(inc_id, c["at"], c["event_id"])
                for r in desk._results(desk.pending.pop()):
                    note(key(r, "checking"), r.get("latency_s", 0), r["published"])
                    if not r["published"]:
                        why[reason(r)] += 1
            desk.decided(inc_id, s["decided"]["at"], ("tv", "stadium"))
            for r in desk.drain():  # one job per card
                note(key(r, "decided"), r.get("latency_s", 0), r["published"])
                if not r["published"]:
                    why[reason(r)] += 1
            print(f"  run {run}/{args.repeats}  {inc_id}  done in {time.perf_counter() - started:.0f} s", flush=True)

    rows = []
    for k, label in STEPS:
        if not times[k]:
            continue
        v = times[k]
        rows.append({"step": label, "runs": len(v), "median_s": round(statistics.median(v), 2),
                     "slowest_s": round(max(v), 2), "shown": f"{shown[k][0]}/{shown[k][1]}"})

    per_agent = defaultdict(list)
    for rec in telemetry.RECORDS:
        if rec.get("gen_ai.agent.name"):
            per_agent[rec["gen_ai.agent.name"]].append(rec["seconds"])
    agents = {a: {"calls": len(v), "median_s": round(statistics.median(v), 2), "slowest_s": round(max(v), 2)}
              for a, v in per_agent.items()}

    print("\n| Card | Runs | Median | Slowest | Shown |")
    print("|---|---|---|---|---|")
    for r in rows:
        ms = lambda x: f"{x * 1000:.0f} ms" if x < 1 else f"{x:.1f} s"
        print(f"| {r['step']} | {r['runs']} | {ms(r['median_s'])} | {ms(r['slowest_s'])} | {r['shown']} |")
    if why:
        print("\nWhy some cards were not shown (the template card stayed on screen instead):")
        for w, n in sorted(why.items(), key=lambda x: -x[1]):
            print(f"  {n} x {w}")
    print("\nEach agent on its own (one run of the agent, all its model calls included):")
    for a, v in agents.items():
        print(f"  {a:<14} {v['calls']} runs, median {v['median_s']:.1f} s, slowest {v['slowest_s']:.1f} s")

    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"repeats": args.repeats, "incidents": len(sig), "cards": rows, "agents": agents,
                               "not_shown": dict(why),
                               "measured": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    print(f"\nSaved to {out}")
    telemetry.flush()


if __name__ == "__main__":
    main()
