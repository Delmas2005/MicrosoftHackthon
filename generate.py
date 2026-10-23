"""Generate a full synthetic match: events, stoppages, tracking, incidents, analysis, heatmaps.

Usage:  python generate.py [--script generator/match_script.json] [--out data] [--seed N]
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

from generator.common import PERIOD_LEN, clock_label, load_script, r2
from generator.events import Engine
from generator.heatmaps import build_heatmaps, incident_occupancy, referee_coverage
from generator.tracking import build_tracking, incident_window
from tools.referee_eye import analyze_incident

GENERATOR_VERSION = "0.1.0"


def dump(path: Path, obj, indent=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=indent, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default="generator/match_script.json")
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    script = load_script(Path(args.script))
    seed = args.seed if args.seed is not None else script["seed"]
    eng = Engine(script, seed).run()
    trk = build_tracking(script, eng, seed)
    out = Path(args.out) / script["match_id"]
    names = {pid: p["name"] for t in script["teams"].values() for pid, p in {**t["starting"], **t["bench"]}.items()}
    names.update({k: v["name"] for k, v in script["officials"].items()})

    # ---------------------------------------------------------------- events
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "events.jsonl", "w") as f:
        for e in eng["events"]:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- stoppages / added time
    by_period = {}
    for p, info in eng["periods"].items():
        led = [l for l in eng["ledger"] if l["period"] == p]
        by_type = {}
        for l in led:
            k = by_type.setdefault(l["type"], {"count": 0, "seconds": 0.0, "chargeable": l["chargeable"]})
            k["count"] += 1
            k["seconds"] = r2(k["seconds"] + l["duration_s"])
        dead = sum(l["duration_s"] for l in led)
        by_period[str(p)] = {
            "period_length_s": info["end_t"],
            "ball_in_play_s": r2(info["end_t"] - dead),
            "dead_ball_s": r2(dead),
            "added_time_announced_min": info["added_time_announced_min"],
            "chargeable_s_before_board": info["chargeable_s_before_board"],
            "chargeable_s_after_board": info["chargeable_s_after_board"],
            "by_type": by_type,
        }
    dump(out / "stoppages.json", {
        "note": "Every dead-ball interval. 'chargeable' intervals are the ones officials add back as added time. "
                "The model adds 75% of chargeable time before the board goes up.",
        "by_period": by_period,
        "ledger": eng["ledger"],
    })

    # ---------------------------------------------------------------- tracking 1 Hz
    with open(out / "tracking_1hz.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["period", "t", "entity", "x", "y", "ball_in_play"])
        for p, P in trk["periods"].items():
            pos, dead = P["pos"], P["dead"]
            for s in range(pos.shape[0]):
                for ent, i in trk["index"].items():
                    x, y = pos[s, i]
                    if np.isnan(x):
                        continue
                    w.writerow([p, s, ent, round(float(x), 2), round(float(y), 2), int(not dead[s])])

    # ---------------------------------------------------------------- incidents
    incident_index = []
    for b in script["scripted"]:
        if "incident" not in b:
            continue
        inc = b["incident"]
        anchor = eng["block_anchor"][b["id"]]
        win = incident_window(script, eng, trk, b)
        analysis = analyze_incident(script, b, win)
        analysis["referee_coverage"] = referee_coverage(trk, b["period"], anchor["t"] + inc["focal"]["dt"], (inc["focal"]["x"], inc["focal"]["y"]))
        analysis["incident_occupancy"] = incident_occupancy(win, inc["focal"]["dt"])
        linked = [e["event_id"] for e in eng["events"] if e.get("incident") == b["id"] or e.get("scripted_block") == b["id"]]
        public = {
            "incident_id": b["id"],
            "period": b["period"],
            "t": anchor["t"],
            "clock": anchor["clock"],
            "type": inc["type"],
            "headline": inc["headline"],
            "attacking_team": inc["attacking_team"],
            "defending_team": inc["defending_team"],
            "involved": {role: {"id": pid, "name": names[pid]} for role, pid in inc["involved"].items()},
            "focal": inc["focal"],
            "critical_window": inc["critical_window"],
            "observers": {o: names.get(o, o) for o in inc["observers"]},
            "officiating": inc["officiating"],
            "laws": inc["ground_truth"]["laws"],
            "linked_events": linked,
            "window": win,
        }
        if "facing_target" in inc:
            public["facing_target"] = inc["facing_target"]
        dump(out / "incidents" / f"{b['id']}.json", public, indent=None)
        dump(out / "ground_truth" / f"{b['id']}.json", {"incident_id": b["id"], **inc["ground_truth"]})
        dump(out / "analysis" / f"{b['id']}.json", analysis)
        incident_index.append({"incident_id": b["id"], "period": b["period"], "clock": anchor["clock"], "type": inc["type"],
                               "headline": inc["headline"], "final_decision": inc["officiating"]["final_decision"]})

    # ---------------------------------------------------------------- heatmaps
    dump(out / "heatmaps.json", build_heatmaps(script, trk), indent=None)

    # ---------------------------------------------------------------- match.json
    teams = {}
    for code, t in script["teams"].items():
        teams[code] = {k: t[k] for k in ("name", "short", "side", "colour", "formation")}
        teams[code]["starting"] = t["starting"]
        teams[code]["bench"] = t["bench"]
    goals = [{"clock": e["clock"], "team": e["team"], "player": e["player"], "name": names[e["player"]],
              "from": e.get("from", "open_play")}
             for e in eng["events"] if e["type"] == "goal" and e.get("status") == "confirmed"]
    dump(out / "match.json", {
        "match_id": script["match_id"],
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "synthetic": True,
        "disclaimer": "Every club, player, official and venue in this file is fictional.",
        "competition": script["competition"],
        "venue": script["venue"],
        "pitch": script["pitch"],
        "coordinates": "metres; x along the length 0..105, y across 0..68; origin at a corner flag",
        "attack_direction": script["attack_direction"],
        "teams": teams,
        "officials": script["officials"],
        "final_score": eng["score"],
        "goals": goals,
        "periods": {str(p): {**v, "end_clock": clock_label(p, v["end_t"]), "regulation_s": PERIOD_LEN} for p, v in eng["periods"].items()},
        "roster_changes": eng["roster_changes"],
        "incidents": incident_index,
        "scripted_sequences": eng["block_anchor"],
        "files": {
            "events.jsonl": "live event feed, one JSON object per line, in time order",
            "stoppages.json": "dead-ball ledger and added-time breakdown",
            "tracking_1hz.csv": "positions of every player, the referee and the ball, once per second",
            "incidents/<id>.json": "what a live system would know about each incident, with 10 Hz positions",
            "analysis/<id>.json": "Referee's Eye tool output for each incident",
            "ground_truth/<id>.json": "correct decision and panel note; for evaluation only, never shown to agents",
            "heatmaps.json": "5 m grids for every player and the referee",
        },
    })
    print(f"wrote {out}  score {eng['score']}  events {len(eng['events'])}  incidents {len(incident_index)}")


if __name__ == "__main__":
    main()
