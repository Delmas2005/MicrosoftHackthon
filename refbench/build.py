"""Build RefBench: N synthetic officiating incidents with rule-derived answers.

    python -m refbench.build --n 500 --seed 7

Writes refbench/data/items.jsonl (what a system sees) and refbench/data/answers.jsonl
(what it is scored against). Systems must never read answers.jsonl.

Every item varies what makes officiating hard: where the referee stands, who is in his line of
sight, how tight the margin is, how far the ball travelled. The live (on-field) call is wrong
more often when the referee could see less, the way it is on a real pitch.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from refbench.adapter import analyze_item
from refbench.rules import ALTERNATIVE, LAW, decide

FAMILIES = ["penalty_contact", "offside", "handball", "dogso", "holding"]
GOAL = (105.0, 34.0)
DTS = [round(-1.6 + 0.2 * k, 1) for k in range(11)]  # 5 Hz from -1.6 s to +0.4 s
HEIGHT = {"foot": 0.1, "lower_leg": 0.3, "knee": 0.5, "upper_body": 1.3}


def mirror(p):
    return (105.0 - p[0], 68.0 - p[1])


class Item:
    def __init__(self, rng: random.Random, attack_dir: int):
        self.rng = rng
        self.dir = attack_dir
        self.ents: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {}  # id -> (pos at dt=0, velocity)
        self.used = {"ATT": set(), "DEF": set()}

    def add(self, ent, pos, vel=(0.0, 0.0)):
        self.ents[ent] = ((round(pos[0], 2), round(pos[1], 2)), vel)
        if "-" in ent:
            team, num = ent.split("-")
            self.used[team].add(int(num))

    def free_id(self, team):
        for n in (2, 3, 4, 5, 6, 7, 8, 10, 11, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23):
            if n not in self.used[team]:
                return f"{team}-{n:02d}"
        raise RuntimeError("out of shirt numbers")

    def bystanders(self, focal, ref, k, on_line_p=0.4, radius=16.0):
        rng = self.rng
        if rng.random() < on_line_p:
            s = rng.uniform(0.25, 0.8)
            px = ref[0] + (focal[0] - ref[0]) * s
            py = ref[1] + (focal[1] - ref[1]) * s
            nx, ny = -(focal[1] - ref[1]), focal[0] - ref[0]
            n = math.hypot(nx, ny) or 1.0
            off = rng.gauss(0, 0.3)
            self.add(self.free_id(rng.choice(["ATT", "DEF"])), (px + nx / n * off, py + ny / n * off),
                     (rng.gauss(0, 1.0), rng.gauss(0, 1.0)))
            k -= 1
        for _ in range(max(k, 0)):
            for _try in range(30):
                p = (focal[0] + rng.uniform(-radius, radius * 0.6), focal[1] + rng.uniform(-radius, radius))
                if 1 <= p[0] <= 104 and 1 <= p[1] <= 67 and all(math.hypot(p[0] - q[0][0], p[1] - q[0][1]) > 1.5 for q in self.ents.values()):
                    self.add(self.free_id(rng.choice(["ATT", "DEF"])), p, (rng.gauss(0, 1.5), rng.gauss(0, 1.5)))
                    break

    def referee(self, focal, dmin=8, dmax=28):
        rng = self.rng
        for _ in range(100):
            d = rng.uniform(dmin, dmax)
            phi = math.radians(rng.uniform(100, 260))
            p = (focal[0] + d * math.cos(phi), focal[1] + d * math.sin(phi))
            if 2 <= p[0] <= 103 and 2 <= p[1] <= 66 and all(math.hypot(p[0] - q[0][0], p[1] - q[0][1]) > 2 for q in self.ents.values()):
                v = ((focal[0] - p[0]) / d * 2.0, (focal[1] - p[1]) / d * 2.0)
                self.add("REF", p, v)
                return p
        self.add("REF", (focal[0] - 15, focal[1]))
        return (focal[0] - 15, focal[1])

    def window(self):
        pos = {}
        for ent, (p, v) in self.ents.items():
            xy = []
            for dt in DTS:
                q = (p[0] + v[0] * dt, p[1] + v[1] * dt)
                if self.dir == -1:
                    q = mirror(q)
                xy.append([round(q[0], 2), round(q[1], 2)])
            pos[ent] = xy
        return {"hz": 5, "t0": 0.0, "dt": DTS, "positions": pos}

    def pt(self, p):
        q = mirror(p) if self.dir == -1 else p
        return {"x": round(q[0], 2), "y": round(q[1], 2)}


def build_one(idx: int, family: str, rng: random.Random) -> tuple[dict, dict]:
    it = Item(rng, rng.choice([1, -1]))
    it.add("DEF-01", (102.5, 34 + rng.uniform(-2, 2)))  # defending keeper
    facts, involved, observers, extra = {}, {}, ["REF"], {}
    height, what, crit = 0.5, "", [-0.2, 0.2]

    if family == "penalty_contact":
        focal = (rng.uniform(90, 101), rng.uniform(21, 47))
        it.add("ATT-09", (focal[0] - 0.25, focal[1] + 0.15), (rng.uniform(4, 8), rng.gauss(0, 1)))
        it.add("DEF-04", (focal[0] + 0.35, focal[1] - 0.2), (rng.uniform(-3, 1), rng.gauss(0, 1)))
        bf = rng.random() < 0.5
        facts = {
            "ball_played_first": bf,
            "contact_after_ball_s": round(rng.uniform(0.05, 0.6), 2) if bf else None,
            "contact_force": rng.choices(["low", "medium", "high"], [0.45, 0.35, 0.2])[0],
            "contact_point": rng.choices(["foot", "lower_leg", "knee", "upper_body"], [0.25, 0.4, 0.15, 0.2])[0],
        }
        involved = {"attacker": "ATT-09", "defender": "DEF-04"}
        height, what = HEIGHT[facts["contact_point"]], f"contact on the attacker's {facts['contact_point'].replace('_', ' ')}"
        ref = it.referee(focal)
        it.bystanders(focal, ref, rng.randint(0, 5))
        crit = [-0.4, 0.2]

    elif family == "offside":
        line = rng.uniform(72, 95)
        margin = round(rng.choice([-1, 1]) * (rng.random() ** 2) * 0.6, 2)
        ay = rng.uniform(16, 52)
        focal = (line + margin, ay)
        it.add("ATT-11", focal, (rng.uniform(3, 8), rng.gauss(0, 1)))
        it.add("DEF-05", (line, ay + rng.choice([-1, 1]) * rng.uniform(2, 12)), (rng.uniform(-1, 3), 0))
        for _ in range(rng.randint(1, 3)):
            it.add(it.free_id("DEF"), (line - rng.uniform(0.6, 6), rng.uniform(12, 56)), (rng.uniform(-1, 3), 0))
        passer = (line - rng.uniform(10, 25), rng.uniform(10, 58))
        it.add("ATT-08", passer, (rng.uniform(0, 4), 0))
        it.add("BALL", passer, (0, 0))
        side = rng.choice([-0.8, 68.8])
        it.add("AR1", (line + rng.gauss(0, 0.5), side), (rng.uniform(0, 3), 0))
        ref = it.referee(focal, 10, 30)
        facts = {"margin_m": margin, "attacker_involved": rng.random() < 0.9, "measured_by": "semi-automated offside line at the moment of the pass"}
        involved = {"attacker": "ATT-11", "passer": "ATT-08", "second_last_defender": "DEF-05"}
        observers = ["AR1", "REF"]
        height, what = 1.2, "the attacker's position at the moment of the pass"

    elif family == "handball":
        cx, cy = rng.uniform(80, 96), rng.choice([rng.uniform(2, 13), rng.uniform(55, 66)])
        tgt = (rng.uniform(97, 101), rng.uniform(28, 40))
        L = math.hypot(tgt[0] - cx, tgt[1] - cy)
        for _ in range(50):
            dist = rng.uniform(1.5, min(14, L - 1))
            contact = (cx + (tgt[0] - cx) * dist / L, cy + (tgt[1] - cy) * dist / L)
            if contact[0] >= 89.0 and 14.2 <= contact[1] <= 53.8:
                break
        ux, uy = (tgt[0] - cx) / L, (tgt[1] - cy) / L
        it.add("ATT-07", (cx - ux * 0.6, cy - uy * 0.6), (ux * 3, uy * 3))
        it.add("DEF-03", (contact[0] + ux * 0.3, contact[1] + uy * 0.3), (rng.gauss(0, 1), rng.gauss(0, 1)))
        it.add("BALL", contact, (0, 0))
        focal = contact
        facts = {
            "arm_position": rng.choices(["by_side", "natural_movement", "raised_above_shoulder", "extended_away"], [0.25, 0.25, 0.25, 0.25])[0],
            "deflected_off_own_body": rng.random() < 0.2,
            "deliberate": rng.random() < 0.05,
            "distance_from_kick_m": round(dist, 1),
        }
        involved = {"attacker": "ATT-07", "defender": "DEF-03"}
        extra["facing_target"] = it.pt((cx, cy))
        height, what = 1.5, "ball against the defender's arm"
        ref = it.referee(focal)
        it.bystanders(focal, ref, rng.randint(0, 4))

    elif family == "dogso":
        D, A = rng.uniform(14, 36), rng.uniform(0, 45)
        sgn = rng.choice([-1, 1])
        a = (GOAL[0] - D * math.cos(math.radians(A)), GOAL[1] + sgn * D * math.sin(math.radians(A)))
        ux, uy = (GOAL[0] - a[0]) / D, (GOAL[1] - a[1]) / D
        ctrl = rng.choices(["controlled", "likely", "unlikely"], [0.6, 0.25, 0.15])[0]
        ahead = {"controlled": 0.8, "likely": 2.5, "unlikely": 5.0}[ctrl]
        it.add("ATT-11", a, (ux * 7.5, uy * 7.5))
        it.add("DEF-05", (a[0] - ux * 0.7, a[1] - uy * 0.7 + 0.3), (ux * 7, uy * 7))
        it.add("BALL", (a[0] + ux * ahead, a[1] + uy * ahead), (ux * 7.5, uy * 7.5))
        if rng.random() < 0.35:
            s = rng.uniform(4, max(5, D - 8))
            lat = rng.uniform(2, 14) * rng.choice([-1, 1])
            p = (a[0] + ux * s - uy * lat, a[1] + uy * s + ux * lat)
            p = (min(max(p[0], 60), 103), min(max(p[1], 3), 65))
            it.add("DEF-04", p, (rng.gauss(0, 2), rng.gauss(0, 2)))
        for _ in range(rng.randint(0, 3)):  # trailing players
            it.add(it.free_id(rng.choice(["ATT", "DEF"])), (a[0] - rng.uniform(5, 20), a[1] + rng.uniform(-12, 12)), (ux * 6, uy * 6))
        focal = (a[0] - ux * 0.3, a[1] - uy * 0.3)
        in_area = a[0] >= 88.5 and 13.84 <= a[1] <= 54.16
        facts = {"distance_to_goal_m": round(D, 1), "angle_to_goal_deg": round(A, 1), "ball_control": ctrl,
                 "in_penalty_area": in_area, "attempt_to_play_ball": in_area and rng.random() < 0.5}
        involved = {"attacker": "ATT-11", "offender": "DEF-05"}
        height, what = 0.3, "the trip on the attacker"
        ref = it.referee(focal, 10, 30)
        crit = [-0.3, 0.2]

    else:  # holding
        focal = (rng.uniform(96, 102), rng.uniform(28, 42))
        it.add("ATT-05", (focal[0] - 0.3, focal[1] + 0.25), (rng.gauss(0, 1), rng.gauss(0, 1)))
        it.add("DEF-06", (focal[0] + 0.3, focal[1] - 0.25), (rng.gauss(0, 1), rng.gauss(0, 1)))
        it.add("BALL", (rng.uniform(98, 103), rng.uniform(30, 42)), (0, 0))
        facts = {"duration_s": round(rng.uniform(0.2, 2.5), 1), "mutual": rng.random() < 0.4,
                 "attacker_impeded": rng.random() < 0.5, "set_piece": rng.choice(["corner", "long_throw", "free_kick"])}
        involved = {"attacker": "ATT-05", "defender": "DEF-06"}
        height, what = 1.2, "holding between the marker and the attacker"
        ref = it.referee(focal, 12, 26)
        it.bystanders(focal, ref, rng.randint(2, 6), on_line_p=0.55, radius=8)
        crit = [-0.6, 0.2]

    if "BALL" not in it.ents:
        it.add("BALL", focal, (0, 0))
    win = it.window()
    item = {
        "item_id": f"RB-{idx:04d}",
        "family": family,
        "attack_direction": it.dir,
        "attacking_team": "ATT",
        "defending_team": "DEF",
        "goalkeepers": ["DEF-01"],
        "involved": involved,
        "focal": {**it.pt(focal), "dt": 0.0, "height_m": height, "what": what},
        "critical_window": crit,
        "observers": observers,
        "facts": facts,
        "law": LAW[family],
        "window": win,
        **extra,
    }
    # geometry that the rules depend on is measured, not invented
    an = analyze_item(item)
    if family == "dogso":
        dc = an["dogso_cover"]
        item["facts"]["covering_defender_can_reach"] = dc["any_cover"]
        item["facts"]["cover_margin_s"] = dc["closest_attempt"]["best_margin_s"] if dc["closest_attempt"] else -9.0
    correct, borderline, factors = decide(family, item["facts"])
    vis = an["observers"]["REF"]["mean_visible_share_in_critical_window"]
    if family == "offside":
        m = abs(item["facts"]["margin_m"])
        p_err = 0.5 if m < 0.1 else 0.25 if m < 0.3 else 0.05
    else:
        p_err = 0.08 + 0.5 * (1 - vis) + (0.15 if borderline else 0.0)
    on_field = ALTERNATIVE[correct] if rng.random() < p_err else correct
    item["on_field_decision"] = on_field
    should = on_field != correct and (family == "offside" or not borderline)
    answer = {
        "item_id": item["item_id"],
        "family": family,
        "correct_decision": correct,
        "borderline": borderline,
        "should_var_intervene": should,
        "final_decision_after_var": correct if should else on_field,
        "key_factors": factors,
        "law": LAW[family],
        "referee_visible_share": vis,
        "best_view_visible_share": an["best_view"]["best"]["visible_share"],
    }
    return item, answer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="refbench/data")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    items, answers = [], []
    for i in range(args.n):
        it, ans = build_one(i + 1, FAMILIES[i % len(FAMILIES)], rng)
        items.append(it)
        answers.append(ans)
    (out / "items.jsonl").write_text("\n".join(json.dumps(x) for x in items) + "\n")
    (out / "answers.jsonl").write_text("\n".join(json.dumps(x) for x in answers) + "\n")
    print(f"wrote {len(items)} items to {out}")


if __name__ == "__main__":
    main()
