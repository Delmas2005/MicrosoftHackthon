"""Sanity checks on a generated match. Exit code 1 if any check fails.

Usage:  python validate.py [data/match_001]
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

D = Path(sys.argv[1] if len(sys.argv) > 1 else "data/match_001")
results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))


match = json.loads((D / "match.json").read_text())
events = [json.loads(l) for l in (D / "events.jsonl").read_text().splitlines()]
stops = json.loads((D / "stoppages.json").read_text())

# score and goals
check("final score is Northbridge 1, Kestrel 2", match["final_score"] == {"NBR": 1, "KES": 2}, str(match["final_score"]))
check("three confirmed goals", len(match["goals"]) == 3, str([g["clock"] for g in match["goals"]]))

# event ordering and ids
order_ok = all((a["period"], a["t"]) <= (b["period"], b["t"]) for a, b in zip(events, events[1:]))
check("events are in time order", order_ok)
check("event ids are unique", len({e["event_id"] for e in events}) == len(events))

# ball in play
bip = sum(v["ball_in_play_s"] for v in stops["by_period"].values()) / 60
check("ball in play between 50 and 60 minutes", 50 <= bip <= 60, f"{bip:.1f} min")
for p, v in stops["by_period"].items():
    check(f"half {p} added time between 1 and 12 minutes", 1 <= v["added_time_announced_min"] <= 12, f"+{v['added_time_announced_min']}")

# every incident at its scripted time
for inc in match["incidents"]:
    seq = match["scripted_sequences"][inc["incident_id"]]
    check(f"{inc['incident_id']} happened at its scripted time", abs(seq["t"] - seq["scheduled_t"]) < 15, inc["clock"])

# no player acts while off the pitch
changes = match["roster_changes"]
on_from, off_from = {}, {}
for c in changes:
    off_from[c["off"]] = (c["period"], c["t"])
    if c["on"]:
        on_from[c["on"]] = (c["period"], c["t"])
starters = {pid for t in match["teams"].values() for pid in t["starting"]}
bad = []
for e in events:
    pid = e.get("player")
    if not pid or not pid[:3] in ("NBR", "KES") or e["type"] in ("substitution", "card", "decision"):
        continue
    key = (e["period"], e["t"])
    if pid not in starters and (pid not in on_from or key < on_from[pid]):
        bad.append((e["event_id"], pid, "not yet on"))
    if pid in off_from and key > off_from[pid]:
        bad.append((e["event_id"], pid, "already off"))
check("no player is involved in an event while off the pitch", not bad, str(bad[:5]))

# 1 Hz speeds and bounds
track = defaultdict(list)
with open(D / "tracking_1hz.csv") as f:
    for r in csv.DictReader(f):
        track[(r["period"], r["entity"])].append((int(r["t"]), float(r["x"]), float(r["y"])))
fast, total, oob = 0, 0, 0
for (p, ent), rows in track.items():
    if ent == "BALL":
        continue
    for (t0, x0, y0), (t1, x1, y1) in zip(rows, rows[1:]):
        if t1 - t0 != 1:
            continue
        total += 1
        if math.hypot(x1 - x0, y1 - y0) > 10.0:
            fast += 1
    oob += sum(1 for _, x, y in rows if not (-2 <= x <= 107 and -2 <= y <= 70))
check("players exceed 10 m/s in under 1% of 1 Hz steps", fast / max(total, 1) < 0.01, f"{fast}/{total} = {fast / max(total, 1):.2%}")
check("no player is off the pitch area", oob == 0, str(oob))

# 10 Hz incident windows: keyframed actors move at human speeds
for inc in match["incidents"]:
    win = json.loads((D / "incidents" / f"{inc['incident_id']}.json").read_text())["window"]
    worst = (0.0, None)
    for ent, xy in win["positions"].items():
        if ent == "BALL":
            continue
        for a, b in zip(xy, xy[1:]):
            v = math.hypot(b[0] - a[0], b[1] - a[1]) * win["hz"]
            if v > worst[0]:
                worst = (v, ent)
    check(f"{inc['incident_id']} fastest player under 11 m/s", worst[0] < 11.0, f"{worst[0]:.1f} m/s ({worst[1]})")

# Referee's Eye outcomes match the scenario design
A = {i: json.loads((D / "analysis" / f"INC-0{i}.json").read_text()) for i in range(1, 6)}
check("INC-01 contact hidden from the referee", A[1]["observers"]["REF"]["mean_visible_share_in_critical_window"] <= 0.2,
      f"visible {A[1]['observers']['REF']['mean_visible_share_in_critical_window']}, blockers {A[1]['observers']['REF']['blockers']}")
check("INC-01 best view finds a clear spot within reach", A[1]["best_view"]["best"]["visible_share"] >= 0.9,
      A[1]["best_view"]["coaching_note"])
off = A[2]["offside"]
check("INC-02 offside by about 9 cm", off["offside_position"] and abs(off["margin_m"] - 0.09) < 0.015, f"{off['margin_m']} m")
check("INC-03 referee looking at the defender's back", A[3]["behind_body"]["observer_behind_defender"],
      f"{A[3]['behind_body']['angle_from_defender_facing_deg']} deg")
check("INC-04 no covering defender", not A[4]["dogso_cover"]["any_cover"], str(A[4]["dogso_cover"]["closest_attempt"]))
check("INC-05 holding hidden from the referee", A[5]["observers"]["REF"]["mean_visible_share_in_critical_window"] <= 0.2,
      f"visible {A[5]['observers']['REF']['mean_visible_share_in_critical_window']}, blockers {A[5]['observers']['REF']['blockers']}")
check("INC-02 assistant had a clear view, the margin was the problem", A[2]["observers"]["AR1"]["visible_share_at_key_moment"] >= 0.9,
      f"{A[2]['offside']['assistant_referee']['margin_as_seen_deg']} deg")

# ground truth kept apart from what agents see
leak = any("ground_truth" in json.loads((D / "incidents" / f"INC-0{i}.json").read_text()) for i in range(1, 6))
check("ground truth is not inside the agent-facing incident files", not leak)

width = max(len(n) for _, n, _ in results)
for ok, name, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
