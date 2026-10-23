"""Event-stream engine: random but plausible play, with scripted sequences spliced in.

Output
------
events  : list of event dicts in time order (the "live feed" the agents ingest)
ledger  : every dead-ball interval with its cause and whether it counts towards added time
periods : start/end, announced added time and roster changes per half
"""
from __future__ import annotations

import math
import random

from .common import (
    PERIOD_LEN, PITCH_L, PITCH_W, RESTART_DELAY, SLOTS,
    clock_label, r2, shot_value, to_pitch_frame, to_team_frame,
)

ON_BALL = {
    "pass", "carry", "cross", "shot", "throw_in", "goal_kick", "corner", "free_kick",
    "kick_off", "clearance", "interception", "tackle", "header_pass", "penalty_kick", "save",
}


class Roster:
    """Who is on the pitch, in which slot, and when that changed."""

    def __init__(self, script: dict):
        self.slot: dict[str, dict[str, str]] = {}
        self.names: dict[str, str] = {}
        for code, team in script["teams"].items():
            self.slot[code] = {pid: p["slot"] for pid, p in team["starting"].items()}
            for pid, p in {**team["starting"], **team["bench"]}.items():
                self.names[pid] = p["name"]
        self.changes: list[dict] = []  # {period, t, team, off, on, slot, reason}

    def team_of(self, pid: str) -> str:
        return pid.split("-")[0]

    def active(self, team: str) -> dict[str, str]:
        return self.slot[team]

    def substitute(self, period, t, team, off, on, slot=None):
        s = slot or self.slot[team].pop(off)
        if slot:
            self.slot[team].pop(off, None)
        self.slot[team][on] = s
        self.changes.append({"period": period, "t": r2(t), "team": team, "off": off, "on": on, "slot": s, "reason": "substitution"})

    def send_off(self, period, t, team, pid):
        s = self.slot[team].pop(pid, None)
        self.changes.append({"period": period, "t": r2(t), "team": team, "off": pid, "on": None, "slot": s, "reason": "red_card"})


def formation_targets(active: dict[str, str], direction: int, ball_xy, in_possession: bool) -> dict[str, tuple[float, float]]:
    """Where each active player of a team 'wants' to be, given the ball (pitch coords)."""
    bu, bv = to_team_frame(ball_xy[0], ball_xy[1], direction)
    shift = (bu - 52.5) * 0.45 + (6.0 if in_possession else -6.0)
    width = 1.0 if in_possession else 0.72
    out = {}
    for pid, slot in active.items():
        su, sv = SLOTS[slot]
        if slot == "GK":
            u = 5.0 + max(0.0, bu - 40.0) * 0.12
            v = 34.0 + (bv - 34.0) * 0.12
        else:
            u = su + shift
            v = 34.0 + (sv - 34.0) * width + (bv - 34.0) * 0.25
            u = min(max(u, 3.0), 100.0)
            v = min(max(v, 2.0), 66.0)
        out[pid] = to_pitch_frame(u, v, direction)
    return out


class Engine:
    def __init__(self, script: dict, seed: int | None = None):
        self.s = script
        self.rng = random.Random(seed if seed is not None else script["seed"])
        self.roster = Roster(script)
        self.events: list[dict] = []
        self.ledger: list[dict] = []
        self.periods: dict[int, dict] = {}
        self.block_anchor: dict[str, dict] = {}
        self.score = {t: 0 for t in script["teams"]}
        self.yellow: set[str] = set()
        self.protected = self._protected_players()
        self.subs_pending = list(script["substitutions"])
        self.blocks = sorted(script["scripted"], key=lambda b: (b["period"], b["t"]))
        self.emitted_blocks: set[str] = set()
        self._eid = 0
        self._sid = 0

    # ------------------------------------------------------------------ helpers
    def _protected_players(self) -> set[str]:
        out = set()
        # Players who commit an offence in a scripted incident must not pick up a random
        # yellow card earlier, or the incident would become a second-yellow situation.
        for b in self.s["scripted"]:
            inc = b.get("incident")
            if inc:
                out.update(v for k, v in inc["involved"].items() if k in ("defender", "offender"))
        return out

    def other(self, team: str) -> str:
        return next(t for t in self.s["teams"] if t != team)

    def dirn(self, team: str) -> int:
        return self.s["attack_direction"][str(self.period)][team]

    def targets(self, team: str, in_poss: bool):
        return formation_targets(self.roster.active(team), self.dirn(team), self.ball, in_poss)

    def nearest(self, team: str, xy, in_poss: bool, exclude=(), outfield_only=True):
        best, bd = None, 1e9
        for pid, (x, y) in self.targets(team, in_poss).items():
            if pid in exclude:
                continue
            if outfield_only and self.roster.active(team)[pid] == "GK":
                continue
            d = math.hypot(x - xy[0], y - xy[1])
            if d < bd:
                best, bd = pid, d
        return best

    def gk(self, team: str) -> str:
        return next(p for p, s in self.roster.active(team).items() if s == "GK")

    def emit(self, type_: str, team: str | None = None, player: str | None = None, x=None, y=None, **kw) -> str:
        self._eid += 1
        eid = f"E{self._eid:05d}"
        ev = {"event_id": eid, "period": self.period, "t": r2(kw.pop("t", self.t)), "clock": None, "type": type_}
        ev["clock"] = clock_label(self.period, ev["t"])
        if team:
            ev["team"] = team
        if player:
            ev["player"] = player
        if x is not None:
            ev["x"], ev["y"] = r2(x), r2(y)
        for k, v in kw.items():
            if v is None:
                continue
            ev[k] = r2(v) if isinstance(v, float) else v
        ev.setdefault("possession", self.poss)
        self.events.append(ev)
        return eid

    def stoppage(self, type_: str, duration: float, chargeable: bool, linked: str | None = None, note: str | None = None):
        self._sid += 1
        self.ledger.append({
            "stoppage_id": f"S{self._sid:04d}", "period": self.period, "type": type_,
            "start_t": r2(self.t), "end_t": r2(self.t + duration), "duration_s": r2(duration),
            "chargeable": chargeable, "linked_event": linked, "note": note,
        })
        self.t += duration

    def clamp_pitch(self, x, y):
        return min(max(x, 0.5), PITCH_L - 0.5), min(max(y, 0.5), PITCH_W - 0.5)

    # ------------------------------------------------------------------ restarts
    def ball_out(self, x, y, last_team: str, last_player: str | None, over: str):
        """Ball leaves the pitch: log it, decide the restart, add the dead time."""
        attacker_dir = self.dirn(last_team)
        if over == "touchline":
            rx, ry = min(max(x, 1.0), PITCH_L - 1.0), (0.0 if y < PITCH_W / 2 else PITCH_W)
            restart = {"type": "throw_in", "team": self.other(last_team), "x": rx, "y": ry}
        else:
            # over a goal line: which goal line?
            at_x = PITCH_L if x > PITCH_L / 2 else 0.0
            defending_team = next(t for t in self.s["teams"] if self.dirn(t) == (-1 if at_x == PITCH_L else 1))
            attacking_team = self.other(defending_team)
            if last_team == attacking_team:
                gx = 5.5 if at_x == 0.0 else PITCH_L - 5.5
                restart = {"type": "goal_kick", "team": defending_team, "x": gx, "y": 34.0 + self.rng.uniform(-6, 6)}
            else:
                cy = 0.4 if y < PITCH_W / 2 else PITCH_W - 0.4
                cx = 0.4 if at_x == 0.0 else PITCH_L - 0.4
                restart = {"type": "corner", "team": attacking_team, "x": cx, "y": cy}
        self.emit("ball_out", last_team, last_player, x, y, out_over=over)
        self.poss = restart["team"]
        lo, hi = RESTART_DELAY[restart["type"]]
        self.stoppage(restart["type"], self.rng.uniform(lo, hi), False)
        self.ball = (restart["x"], restart["y"])
        self.pending = restart
        _ = attacker_dir

    def maybe_substitutions(self):
        due = [s for s in self.subs_pending if s["period"] == self.period and s["not_before_t"] <= self.t]
        if not due:
            return
        for s in due:
            self.subs_pending.remove(s)
            eid = self.emit("substitution", s["team"], s["on"], replaces=s["off"])
            self.roster.substitute(self.period, self.t, s["team"], s["off"], s["on"], s.get("slot"))
            self.stoppage("substitution", self.rng.uniform(22, 32), True, eid)

    def do_restart(self):
        r = self.pending
        self.pending = None
        self.maybe_substitutions()
        team, typ = r["team"], r["type"]
        self.poss = team
        self.ball = (r["x"], r["y"])
        d = self.dirn(team)
        if typ == "kick_off":
            p = r.get("player") or self.nearest(team, self.ball, True)
            rec = next((q for q, s in self.roster.active(team).items() if s in ("DM", "RCM", "LCM")), None)
            tx, ty = self.targets(team, True)[rec]
            self._complete_pass("kick_off", team, p, rec, (tx, ty))
        elif typ == "throw_in":
            p = r.get("player") or self.nearest(team, self.ball, True)
            u, v = to_team_frame(*self.ball, d)
            end = to_pitch_frame(u + self.rng.uniform(-4, 10), v + (self.rng.uniform(6, 14) if v < 34 else -self.rng.uniform(6, 14)), d)
            rec = self.nearest(team, end, True, exclude=(p,))
            self._complete_pass("throw_in", team, p, rec, end)
        elif typ == "goal_kick":
            p = self.gk(team)
            if self.rng.random() < 0.45:
                rec = next((q for q, s in self.roster.active(team).items() if s in ("ST", "RW", "LW")), None)
                self._pass(team, p, forced_receiver=rec, type_="goal_kick", long=True)
            else:
                rec = next((q for q, s in self.roster.active(team).items() if s in ("RCB", "LCB")), None)
                self._complete_pass("goal_kick", team, p, rec, self.targets(team, True)[rec])
        elif typ == "corner":
            p = r.get("player") or self.nearest(team, self.ball, True)
            self._cross(team, p, type_="corner")
        elif typ == "free_kick":
            p = r.get("player") or self.nearest(team, self.ball, True)
            u, v = to_team_frame(*self.ball, d)
            if u > 72 and abs(v - 34) < 22 and r.get("kind") != "indirect" and self.rng.random() < 0.5:
                self._shot(team, p, type_="free_kick")
            elif u > 70 and self.rng.random() < 0.6:
                self._cross(team, p, type_="free_kick")
            else:
                self._pass(team, p, type_="free_kick")
        self.carrier_hint = None

    # ------------------------------------------------------------------ on-ball actions
    def _complete_pass(self, type_, team, p, rec, end):
        dist = math.hypot(end[0] - self.ball[0], end[1] - self.ball[1])
        travel = dist / 15.0 + 0.3
        self.emit(type_, team, p, *self.ball, end_x=end[0], end_y=end[1], t_end=self.t + travel,
                  receiver=rec, outcome="complete", possession=team)
        self.t += travel + self.rng.uniform(0.8, 2.2)
        self.ball, self.carrier, self.poss = end, rec, team

    def _pass(self, team, p, forced_receiver=None, type_="pass", long=False):
        d = self.dirn(team)
        bu, bv = to_team_frame(*self.ball, d)
        tg = self.targets(team, True)
        if forced_receiver:
            rec = forced_receiver
        else:
            cands, weights = [], []
            for q, (x, y) in tg.items():
                if q == p:
                    continue
                if self.roster.active(team)[q] == "GK" and bu > 30:
                    continue
                qu, qv = to_team_frame(x, y, d)
                dist = math.hypot(qu - bu, qv - bv)
                fwd = qu - bu
                w = math.exp(-((dist - 15) ** 2) / (2 * 9**2)) * (1 + 0.02 * max(fwd, 0)) * (0.7 if fwd < -10 else 1.0)
                cands.append(q)
                weights.append(w)
            rec = self.rng.choices(cands, weights)[0]
        tu, tv = to_team_frame(*tg[rec], d)
        tu += self.rng.gauss(0, 2.0)
        tv += self.rng.gauss(0, 2.0)
        dist = math.hypot(tu - bu, tv - bv)
        p_ok = 0.95 - 0.009 * dist - (0.10 if tu > 85 else 0.0) - (0.15 if long else 0.0)
        p_ok = min(max(p_ok, 0.4), 0.95)
        if self.rng.random() < p_ok:
            self._complete_pass(type_, team, p, rec, to_pitch_frame(min(max(tu, 1), 104), min(max(tv, 1), 67), d))
            return
        # failed pass: wayward or intercepted
        eu, ev = tu + self.rng.gauss(0, 4), tv + self.rng.gauss(0, 4)
        if (tv < 12 or tv > 56) and self.rng.random() < 0.6:
            ev = -1.5 if tv < 34 else PITCH_W + 1.5
        if tu > 92 and self.rng.random() < 0.2:
            eu = PITCH_L + 1.5
        ex, ey = to_pitch_frame(eu, ev, d)
        travel = math.hypot(eu - bu, ev - bv) / 15.0 + 0.3
        if ev < 0 or ev > PITCH_W:
            self.emit(type_, team, p, *self.ball, end_x=ex, end_y=ey, t_end=self.t + travel, receiver=rec, outcome="out")
            self.t += travel
            self.ball_out(ex, ey, team, p, "touchline")
            return
        if eu > PITCH_L:
            self.emit(type_, team, p, *self.ball, end_x=ex, end_y=ey, t_end=self.t + travel, receiver=rec, outcome="out")
            self.t += travel
            self.ball_out(ex, ey, team, p, "goal_line")
            return
        ex, ey = self.clamp_pitch(ex, ey)
        opp = self.other(team)
        frac = self.rng.uniform(0.55, 0.95)
        ix, iy = self.ball[0] + (ex - self.ball[0]) * frac, self.ball[1] + (ey - self.ball[1]) * frac
        interceptor = self.nearest(opp, (ix, iy), False)
        self.emit(type_, team, p, *self.ball, end_x=ix, end_y=iy, t_end=self.t + travel * frac, receiver=rec,
                  outcome="intercepted", possession=opp)
        self.t += travel * frac
        self.poss, self.ball, self.carrier = opp, (ix, iy), interceptor
        self.emit("interception", opp, interceptor, ix, iy, possession=opp)
        self.t += self.rng.uniform(0.6, 1.4)

    def _carry(self, team, p):
        d = self.dirn(team)
        bu, bv = to_team_frame(*self.ball, d)
        dist = self.rng.uniform(2, 9)
        nu = min(bu + dist, 100.0)
        nv = min(max(bv + self.rng.gauss(0, 3), 2), 66)
        end = to_pitch_frame(nu, nv, d)
        dur = math.hypot(nu - bu, nv - bv) / 5.5 + 0.2
        self.emit("carry", team, p, *self.ball, end_x=end[0], end_y=end[1], t_end=self.t + dur)
        self.t += dur
        self.ball = end

    def _shot(self, team, p, type_="shot", body_part=None):
        d = self.dirn(team)
        opp = self.other(team)
        gx = PITCH_L if d == 1 else 0.0
        xg = shot_value(*self.ball, d)
        roll = self.rng.random()
        dist = math.hypot(gx - self.ball[0], 34 - self.ball[1])
        travel = dist / 24.0
        bp = body_part or ("head" if type_ == "header" else self.rng.choice(["right_foot", "right_foot", "left_foot"]))
        if type_ == "free_kick":
            self.emit("free_kick", team, p, *self.ball, kind="direct", outcome="shot")
        if roll < 0.28:  # blocked
            bx = self.ball[0] + (gx - self.ball[0]) * 0.15
            by = self.ball[1] + self.rng.uniform(-2, 2)
            blocker = self.nearest(opp, (bx, by), False)
            self.emit("shot", team, p, *self.ball, end_x=bx, end_y=by, t_end=self.t + 0.25, outcome="blocked",
                      xg=xg, body_part=bp, blocked_by=blocker)
            self.t += 0.25
            self.ball = (bx, by)
            if self.rng.random() < 0.45:
                cy = by + self.rng.uniform(-8, 8)
                self.t += 1.0
                self.ball_out(gx + (1.0 if d == 1 else -1.0), cy, opp, blocker, "goal_line")
            else:
                winner = team if self.rng.random() < 0.45 else opp
                pid = self.nearest(winner, self.ball, winner == team)
                self.poss, self.carrier = winner, pid
                self.t += 1.2
                self.emit("ball_recovery", winner, pid, *self.ball, possession=winner)
        elif roll < 0.65:  # off target
            ey = 34 + self.rng.choice([-1, 1]) * self.rng.uniform(4.2, 9)
            ex = gx + (1.0 if d == 1 else -1.0)
            self.emit("shot", team, p, *self.ball, end_x=ex, end_y=ey, t_end=self.t + travel, outcome="off_target", xg=xg, body_part=bp)
            self.t += travel
            self.ball_out(ex, ey, team, p, "goal_line")
        else:  # saved
            ey = 34 + self.rng.uniform(-3.2, 3.2)
            ex = gx - (0.8 if d == 1 else -0.8)
            keeper = self.gk(opp)
            self.emit("shot", team, p, *self.ball, end_x=ex, end_y=ey, t_end=self.t + travel, outcome="saved", xg=xg, body_part=bp)
            self.t += travel
            if self.rng.random() < 0.35:
                self.emit("save", opp, keeper, ex, ey, outcome="parried_out", possession=opp)
                self.ball_out(gx + (1.0 if d == 1 else -1.0), ey + self.rng.uniform(-5, 5), opp, keeper, "goal_line")
            else:
                hold = self.rng.uniform(3.0, 7.5)
                self.emit("save", opp, keeper, ex, ey, outcome="held", gk_hold_s=hold, possession=opp)
                self.poss, self.carrier, self.ball = opp, keeper, (ex, ey)
                self.t += hold

    def _cross(self, team, p, type_="cross"):
        d = self.dirn(team)
        opp = self.other(team)
        tu, tv = self.rng.uniform(95, 101), self.rng.uniform(27, 41)
        end = to_pitch_frame(tu, tv, d)
        dist = math.hypot(end[0] - self.ball[0], end[1] - self.ball[1])
        travel = dist / 18.0 + 0.2
        roll = self.rng.random()
        if roll < 0.45:  # cleared
            clearer = self.nearest(opp, end, False)
            self.emit(type_, team, p, *self.ball, end_x=end[0], end_y=end[1], t_end=self.t + travel, outcome="cleared", possession=team)
            self.t += travel
            cu, cv = tu - self.rng.uniform(15, 30), tv + self.rng.uniform(-12, 12)
            cend = self.clamp_pitch(*to_pitch_frame(cu, cv, d))
            self.emit("clearance", opp, clearer, *end, end_x=cend[0], end_y=cend[1], t_end=self.t + 1.4, body_part="head", possession=opp)
            self.t += 1.4
            self.ball = cend
            winner = opp if self.rng.random() < 0.6 else team
            self.poss = winner
            self.carrier = self.nearest(winner, cend, winner == team)
            self.emit("ball_recovery", winner, self.carrier, *cend, possession=winner)
            self.t += 0.8
        elif roll < 0.70:  # header at goal
            header = self.nearest(team, end, True, exclude=(p,))
            self.emit(type_, team, p, *self.ball, end_x=end[0], end_y=end[1], t_end=self.t + travel, receiver=header, outcome="complete")
            self.t += travel
            self.ball = end
            self._shot(team, header, body_part="head")
        elif roll < 0.85:  # keeper claims
            keeper = self.gk(opp)
            self.emit(type_, team, p, *self.ball, end_x=end[0], end_y=end[1], t_end=self.t + travel, outcome="claimed", possession=opp)
            self.t += travel
            hold = self.rng.uniform(3.0, 7.0)
            self.emit("claim", opp, keeper, *end, gk_hold_s=hold, possession=opp)
            self.poss, self.carrier, self.ball = opp, keeper, end
            self.t += hold
        else:  # over everyone and out
            gx = PITCH_L + 1.0 if d == 1 else -1.0
            self.emit(type_, team, p, *self.ball, end_x=gx, end_y=end[1], t_end=self.t + travel, outcome="out")
            self.t += travel
            self.ball_out(gx, end[1], team, p, "goal_line")

    def _foul(self, team, p):
        opp = self.other(team)
        offender = self.nearest(opp, self.ball, False)
        eid = self.emit("foul", opp, offender, *self.ball, opponent=p, on_field_call="foul", possession=team)
        d = self.dirn(team)
        u, _ = to_team_frame(*self.ball, d)
        lo, hi = RESTART_DELAY["free_kick_attacking" if u > 65 else "free_kick"]
        if offender not in self.protected and offender not in self.yellow and self.rng.random() < 0.18:
            self.yellow.add(offender)
            cid = self.emit("card", opp, offender, card="yellow", reason="reckless challenge", linked_event=eid)
            self.stoppage("card", self.rng.uniform(10, 20), True, cid)
        if self.rng.random() < 0.05:
            iid = self.emit("injury_stoppage", team, p, *self.ball)
            self.stoppage("injury", self.rng.uniform(40, 90), True, iid)
        self.stoppage("free_kick", self.rng.uniform(lo, hi), False, eid)
        self.pending = {"type": "free_kick", "team": team, "x": self.ball[0], "y": self.ball[1]}

    def play_action(self):
        team = self.poss
        opp = self.other(team)
        d = self.dirn(team)
        p = self.carrier if self.carrier in self.roster.active(team) else self.nearest(team, self.ball, True)
        self.carrier = p
        u, v = to_team_frame(*self.ball, d)
        r = self.rng.random()
        if self.rng.random() < 0.22:
            presser = self.nearest(opp, self.ball, False)
            self.emit("pressure", opp, presser, *self.ball, on_player=p)
        if u < 80 and r < 0.03:
            self._foul(team, p)
            return
        if r < 0.09:
            tackler = self.nearest(opp, self.ball, False)
            self.emit("tackle", opp, tackler, *self.ball, opponent=p, outcome="ball_won", possession=opp)
            self.poss, self.carrier = opp, tackler
            self.t += self.rng.uniform(0.8, 1.6)
            return
        if u > 83 and abs(v - 34) < 20 and self.rng.random() < 0.17:
            self._shot(team, p)
        elif u > 72 and (v < 15 or v > 53) and self.rng.random() < 0.35:
            self._cross(team, p)
        elif self.rng.random() < 0.22:
            self._carry(team, p)
        elif self.roster.active(team).get(p) == "GK" and self.rng.random() < 0.45:
            rec = next((q for q, s in self.roster.active(team).items() if s in ("ST", "RW", "LW")), None)
            self._pass(team, p, forced_receiver=rec, long=True)
        else:
            self._pass(team, p)

    # ------------------------------------------------------------------ scripted blocks
    def block_start(self, b) -> float:
        return b["t"] + min(e["dt"] for e in b["events"])

    def lead_in(self, b):
        """Steer play so the scripted block starts from a sensible situation."""
        if self.pending:
            self.do_restart()
        team = b["team"]
        first = b["events"][0]
        start = self.block_start(b)
        if self.poss != team:
            pid = self.nearest(team, self.ball, False)
            self.emit("interception", team, pid, *self.ball, possession=team)
            self.poss, self.carrier = team, pid
            self.t += 1.0
        remaining = start - self.t
        n = max(0, int(remaining // 4.5))
        for i in range(n):
            frac = (i + 1) / (n + 1)
            wx = self.ball[0] + (first["x"] - self.ball[0]) * frac
            wy = self.ball[1] + (first["y"] - self.ball[1]) * frac
            rec = self.nearest(team, (wx, wy), True, exclude=(self.carrier,))
            dist = math.hypot(wx - self.ball[0], wy - self.ball[1])
            travel = dist / 15.0 + 0.3
            self.emit("pass", team, self.carrier, *self.ball, end_x=wx, end_y=wy, t_end=self.t + travel,
                      receiver=rec, outcome="complete", possession=team)
            self.t = min(self.t + max(travel + 1.0, (start - self.t) / (n - i + 1)), start - 0.5)
            self.ball, self.carrier = (wx, wy), rec

    def emit_block(self, b):
        min_dt = min(e["dt"] for e in b["events"])
        anchor = max(b["t"], self.t - min_dt + 0.3)
        self.block_anchor[b["id"]] = {"period": b["period"], "t": r2(anchor), "scheduled_t": b["t"],
                                      "clock": clock_label(b["period"], anchor)}
        self.poss = b["team"]
        last_t = anchor
        for e in b["events"]:
            ev = {k: v for k, v in e.items() if k not in ("dt", "dt_end", "until_dt", "stoppage")}
            t = anchor + e["dt"]
            if "dt_end" in e:
                ev["t_end"] = anchor + e["dt_end"]
                last_t = max(last_t, ev["t_end"])
            if "until_dt" in e:
                ev["until_t"] = anchor + e["until_dt"]
            last_t = max(last_t, t)
            typ = ev.pop("type")
            team, player = ev.pop("team", None), ev.pop("player", None)
            x, y = ev.pop("x", None), ev.pop("y", None)
            ev.setdefault("scripted_block", b["id"])
            if typ in ON_BALL and e.get("outcome") not in ("ball_played",):
                ev.setdefault("possession", team)
            eid = self.emit(typ, team, player, x, y, t=t, **ev)
            if typ == "goal" and e.get("status") == "confirmed":
                self.score[team] += 1
            if "stoppage" in e:
                st = e["stoppage"]
                self.t = t
                self.stoppage(st["type"], anchor + st["until_dt"] - t, st["chargeable"], eid)
        self.t = last_t
        last = b["events"][-1]
        if "end_x" in last:
            self.ball = (last["end_x"], last["end_y"])
        elif "x" in last:
            self.ball = (last["x"], last["y"])
        for item in b.get("after", []):
            if "stoppage" in item:
                eid = None
                if "event" in item:
                    ev = dict(item["event"])
                    eid = self.emit(ev.pop("type"), ev.pop("team", None), ev.pop("player", None),
                                    duration_s=item["duration"], scripted_block=b["id"], **ev)
                self.stoppage(item["stoppage"], item["duration"], item["chargeable"], eid)
            elif "event" in item:
                ev = dict(item["event"])
                typ = ev.pop("type")
                team, player = ev.pop("team", None), ev.pop("player", None)
                x, y = ev.pop("x", None), ev.pop("y", None)
                if "travel_s" in ev:
                    ev["t_end"] = self.t + ev.pop("travel_s")
                self.emit(typ, team, player, x, y, scripted_block=b["id"], **ev)
                if typ == "goal" and ev.get("status") == "confirmed":
                    self.score[team] += 1
                if typ == "card" and ev.get("card") == "red":
                    self.roster.send_off(self.period, self.t, team, player)
                self.t += item.get("advance", 0.0)
            elif "restart" in item:
                self.pending = dict(item["restart"])
                self.do_restart()
            elif "continue" in item:
                c = item["continue"]
                self.poss, self.ball, self.carrier = c["team"], (c["x"], c["y"]), c.get("player")
                self.t += 0.8
        self.emitted_blocks.add(b["id"])

    # ------------------------------------------------------------------ main loop
    def chargeable_since(self, period: int, t0: float) -> float:
        return sum(s["duration_s"] for s in self.ledger if s["period"] == period and s["chargeable"] and s["start_t"] >= t0)

    def run(self) -> dict:
        for period in (1, 2):
            self.period, self.t = period, 0.0
            self.ball, self.carrier = (52.5, 34.0), None
            ko = self.s["kick_off"][str(period)]
            self.poss = ko
            st = next(p for p, s in self.roster.active(ko).items() if s == "ST")
            self.emit("period_start", ko)
            self.pending = {"type": "kick_off", "team": ko, "x": 52.5, "y": 34.0, "player": st}
            added, end_t = None, None
            blocks = [b for b in self.blocks if b["period"] == period]
            while True:
                nxt = next((b for b in blocks if b["id"] not in self.emitted_blocks), None)
                if nxt and self.t >= self.block_start(nxt) - 18:
                    self.lead_in(nxt)
                    self.emit_block(nxt)
                    continue
                if added is None and self.t >= PERIOD_LEN:
                    ch = self.chargeable_since(period, 0.0)
                    # Officials add most, not all, of the time lost; 0.75 is this model's calibration.
                    added = max(1, int(0.75 * ch / 60 + 0.5))
                    self.emit("added_time_board", None, None, minutes=added, chargeable_s=ch)
                if added is not None:
                    end_t = PERIOD_LEN + added * 60 + self.chargeable_since(period, PERIOD_LEN)
                    if self.t >= end_t and nxt is None:
                        break
                if self.pending:
                    self.do_restart()
                else:
                    self.play_action()
            self.emit("period_end", None)
            self.periods[period] = {
                "period": period, "end_t": r2(self.t), "added_time_announced_min": added,
                "chargeable_s_before_board": r2(self.chargeable_since(period, 0.0) - self.chargeable_since(period, PERIOD_LEN)),
                "chargeable_s_after_board": r2(self.chargeable_since(period, PERIOD_LEN)),
            }
        return {
            "events": self.events,
            "ledger": self.ledger,
            "periods": self.periods,
            "roster_changes": self.roster.changes,
            "block_anchor": self.block_anchor,
            "score": self.score,
        }
