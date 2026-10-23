"""Player, referee and ball positions.

* 1 Hz for the whole match: players drift around their formation slots as the ball moves,
  the nearest defenders press, the ball carrier follows the ball, and every event pins its
  player to the event location. Good enough for heatmaps and match context.
* 10 Hz around each scripted incident: key actors follow hand-placed keyframes exactly,
  everyone else is interpolated from the 1 Hz track. This is what the Referee's Eye uses.
"""
from __future__ import annotations

import math

import numpy as np

from .common import PITCH_L, PITCH_W, catmull_rom, to_pitch_frame
from .events import ON_BALL, formation_targets

RESTART_TYPES = {"kick_off", "throw_in", "goal_kick", "corner", "free_kick", "penalty_kick"}
VMAX_PLAYER = 7.0   # metres per 1 s step
VMAX_REF = 7.5
BLEND_S = 3.0       # seconds over which event anchors blend into the free simulation
KEY_BLEND_S = 6.0   # incident keyframes can sit far from the free simulation, so blend them in more gently


def entity_list(script: dict) -> list[str]:
    ids = []
    for team in script["teams"].values():
        ids += list(team["starting"]) + list(team["bench"])
    return ids + ["REF", "BALL"]


def active_timeline(script: dict, changes: list[dict]):
    """Return f(period, t) -> {team: {pid: slot}} honouring subs and red cards."""
    start = {code: {pid: p["slot"] for pid, p in t["starting"].items()} for code, t in script["teams"].items()}

    def at(period: int, t: float):
        cur = {k: dict(v) for k, v in start.items()}
        for c in changes:
            if (c["period"], c["t"]) <= (period, t):
                cur[c["team"]].pop(c["off"], None)
                if c["on"]:
                    cur[c["team"]][c["on"]] = c["slot"]
        return cur
    return at


def set_piece_layout(att_active, def_active, att_dir, taker, ball):
    """Box positions for corners and long throws (attacking team frame: goal at u = 105)."""
    att_spots = [(99.0, 30.5), (99.5, 37.5), (97.0, 34.0), (94.0, 27.0), (93.5, 41.0), (84.0, 34.0), (82.0, 20.0)]
    out = {}
    order = sorted((p for p, s in att_active.items() if s != "GK" and p != taker),
                   key=lambda p: ["LCB", "RCB", "ST", "DM", "LCM", "RCM", "LW", "RW", "LB", "RB"].index(att_active[p])
                   if att_active[p] in ["LCB", "RCB", "ST", "DM", "LCM", "RCM", "LW", "RW", "LB", "RB"] else 99)
    for pid, spot in zip(order, att_spots):
        out[pid] = to_pitch_frame(*spot, att_dir)
    for pid in order[len(att_spots):]:
        out[pid] = to_pitch_frame(55.0, 34.0, att_dir)
    gk = next((p for p, s in att_active.items() if s == "GK"), None)
    if gk:
        out[gk] = to_pitch_frame(30.0, 34.0, att_dir)
    if taker:
        out[taker] = ball
    defenders = [p for p, s in def_active.items() if s != "GK"]
    mark_spots = [(u + 0.9, v + (0.4 if v < 34 else -0.4)) for u, v in att_spots[:5]] + [(101.5, 32.0), (101.5, 36.5), (90.0, 34.0), (88.0, 24.0), (75.0, 34.0)]
    for pid, spot in zip(defenders, mark_spots):
        out[pid] = to_pitch_frame(*spot, att_dir)
    dgk = next((p for p, s in def_active.items() if s == "GK"), None)
    if dgk:
        out[dgk] = to_pitch_frame(103.5, 34.0, att_dir)
    return out


def referee_target(ball, attack_dir):
    x = min(max(ball[0] - 11.0 * attack_dir, 16.0), 89.0)
    y_diag = 10.0 + (x - 18.0) * (48.0 / 69.0)
    y = 0.65 * y_diag + 0.35 * ball[1]
    dx, dy = x - ball[0], y - ball[1]
    d = math.hypot(dx, dy)
    if d < 10.0:
        if d < 1e-6:
            dx, dy, d = -attack_dir, 0.0, 1.0
        x, y = ball[0] + dx / d * 10.0, ball[1] + dy / d * 10.0
    return x, y


def build_tracking(script: dict, eng: dict, seed: int = 0):
    rng = np.random.default_rng(seed)
    ents = entity_list(script)
    idx = {e: i for i, e in enumerate(ents)}
    active_at = active_timeline(script, eng["roster_changes"])
    teams = list(script["teams"])
    out = {}
    for period in (1, 2):
        dirs = script["attack_direction"][str(period)]
        T = int(math.ceil(eng["periods"][period]["end_t"])) + 1
        secs = np.arange(T, dtype=float)
        evs = [e for e in eng["events"] if e["period"] == period]
        led = [l for l in eng["ledger"] if l["period"] == period]

        # ball path from event waypoints
        wp = []
        for e in evs:
            if "x" in e:
                wp.append((e["t"], e["x"], e["y"]))
            if "end_x" in e and "t_end" in e:
                wp.append((e["t_end"], e["end_x"], e["end_y"]))
        wp.sort(key=lambda w: w[0])
        wt = np.array([w[0] for w in wp])
        ball = np.stack([np.interp(secs, wt, [w[1] for w in wp]), np.interp(secs, wt, [w[2] for w in wp])], axis=1)

        # possession per second
        poss_t = np.array([e["t"] for e in evs if e.get("possession")])
        poss_v = [e["possession"] for e in evs if e.get("possession")]

        def poss_at(t):
            i = np.searchsorted(poss_t, t, side="right") - 1
            return poss_v[max(i, 0)]

        dead = np.zeros(T, dtype=bool)
        for l in led:
            dead[int(l["start_t"]):int(math.ceil(l["end_t"])) + 1] = True

        # set-piece windows: corners, long throws, attacking crosses from free kicks
        sp_windows = []
        for e in evs:
            if e["type"] == "corner" or (e["type"] == "throw_in" and e.get("long_throw")):
                sp_windows.append((e["t"] - 12.0, e["t"] + 3.0, e["team"], e.get("player")))

        # who is on the ball / receiving it
        onball = [(e["t"], e.get("t_end", e["t"]), e.get("player"), e.get("receiver"), e.get("end_x"), e.get("end_y"), e.get("outcome"))
                  for e in evs if e["type"] in ON_BALL and e.get("player")]
        restarts = [(e["t"], e["player"], e["x"], e["y"]) for e in evs if e["type"] in RESTART_TYPES and e.get("player")]

        pos = np.full((T, len(ents), 2), np.nan)
        noise = np.zeros((len(ents), 2))
        prev = {}
        oi = 0
        for s in range(T):
            b = ball[s]
            pt = poss_at(s)
            act = active_at(period, s)
            sp = next((w for w in sp_windows if w[0] <= s <= w[1]), None)
            targets = {}
            for team in teams:
                targets.update(formation_targets(act[team], dirs[team], b, team == pt))
            if sp:
                att = sp[2]
                dfn = next(t for t in teams if t != att)
                targets.update(set_piece_layout(act[att], act[dfn], dirs[att], sp[3], tuple(b)))
            elif not dead[s]:
                dfn = next(t for t in teams if t != pt)
                outf = [(p, math.hypot(targets[p][0] - b[0], targets[p][1] - b[1])) for p, sl in act[dfn].items() if sl != "GK"]
                outf.sort(key=lambda z: z[1])
                gx = 0.0 if dirs[dfn] == 1 else PITCH_L
                for rank, (p, _) in enumerate(outf[:2]):
                    back = 1.8 if rank == 0 else 6.0
                    dx = gx - b[0]
                    targets[p] = (b[0] + math.copysign(back, dx), b[1] + (0 if rank == 0 else (34 - b[1]) * 0.15))
            # ball carrier and receiver
            while oi + 1 < len(onball) and onball[oi + 1][0] <= s:
                oi += 1
            if onball and onball[oi][0] <= s:
                t0, t1, pl, rec, ex, ey, outc = onball[oi]
                if s <= t1 and rec and ex is not None and outc == "complete":
                    targets[rec] = (ex, ey)
                elif s - t1 < 3.0 and pl in targets:
                    targets[pl] = tuple(b)
            nxt_r = next((r for r in restarts if 0 <= r[0] - s <= 8.0), None)
            if dead[s] and nxt_r and nxt_r[1] in targets:
                targets[nxt_r[1]] = (nxt_r[2], nxt_r[3])
            # integrate
            for pid, tgt in targets.items():
                i = idx[pid]
                noise[i] = 0.7 * noise[i] + rng.normal(0, 0.5, 2)
                goal = np.array(tgt) + noise[i]
                if pid not in prev:
                    p_new = goal
                else:
                    step = goal - prev[pid]
                    n = np.linalg.norm(step)
                    p_new = prev[pid] + (step if n <= VMAX_PLAYER else step / n * VMAX_PLAYER)
                p_new = np.clip(p_new, [-1.5, -1.5], [PITCH_L + 1.5, PITCH_W + 1.5])
                pos[s, i] = p_new
                prev[pid] = p_new
            for pid in list(prev):
                if pid not in targets and pid != "REF":
                    prev.pop(pid)
            # referee
            ri = idx["REF"]
            rt = np.array(referee_target(b, dirs[pt]))
            if "REF" not in prev:
                prev["REF"] = np.array([52.5, 24.0])
            step = rt - prev["REF"]
            n = np.linalg.norm(step)
            prev["REF"] = prev["REF"] + (step if n <= VMAX_REF else step / n * VMAX_REF)
            pos[s, ri] = prev["REF"]
            pos[s, idx["BALL"]] = b

        # event anchors: pin the actor (and a pass receiver) to where the event says they were
        anchors = []
        for e in evs:
            if e.get("player") in idx and "x" in e and e["type"] not in ("ball_out", "goal", "substitution", "card", "announcement", "decision"):
                anchors.append((e["t"], e["player"], e["x"], e["y"]))
            if e.get("receiver") in idx and e.get("outcome") == "complete" and "t_end" in e:
                anchors.append((e["t_end"], e["receiver"], e["end_x"], e["end_y"]))
        _apply_anchors(pos, idx, anchors, T)

        # incident keyframes override everything around the incident
        locked = np.zeros((T, len(ents)), dtype=bool)
        for b in script["scripted"]:
            if b["period"] != period or "incident" not in b:
                continue
            t0 = eng["block_anchor"][b["id"]]["t"]
            for ent, keys in b["incident"]["keyframes"].items():
                if ent not in idx:
                    continue
                i = idx[ent]
                k0, k1 = t0 + keys[0][0], t0 + keys[-1][0]
                for s in range(max(0, int(k0 - KEY_BLEND_S)), min(T, int(k1 + KEY_BLEND_S) + 1)):
                    if np.isnan(pos[s, i, 0]):
                        continue
                    w = 1.0 if k0 <= s <= k1 else max(0.0, 1 - min(abs(s - k0), abs(s - k1)) / KEY_BLEND_S)
                    w = w * w * (3 - 2 * w)  # smoothstep: no sudden change of pace at either end
                    kx, ky = catmull_rom(keys, s - t0)
                    pos[s, i] = (1 - w) * pos[s, i] + w * np.array([kx, ky])
                    locked[s, i] = k0 <= s <= k1

        _limit_speed(pos, locked, idx["BALL"])
        out[period] = {"pos": pos, "secs": secs, "dead": dead}
    return {"entities": ents, "index": idx, "periods": out}


def _limit_speed(pos, locked, ball_i, vcap=9.0):
    """Forward then backward pass that caps running speed, never touching keyframed seconds."""
    T, E, _ = pos.shape
    for i in range(E):
        if i == ball_i:
            continue
        for rng_ in (range(1, T), range(T - 2, -1, -1)):
            for s in rng_:
                prev = s - 1 if rng_.step == 1 else s + 1
                if locked[s, i] or np.isnan(pos[s, i, 0]) or np.isnan(pos[prev, i, 0]):
                    continue
                step = pos[s, i] - pos[prev, i]
                n = float(np.hypot(*step))
                if n > vcap:
                    pos[s, i] = pos[prev, i] + step / n * vcap


def _apply_anchors(pos, idx, anchors, T):
    for t, pid, x, y in anchors:
        i = idx[pid]
        s0 = min(max(int(round(t)), 0), T - 1)
        if np.isnan(pos[s0, i, 0]):
            continue
        # the further the free simulation drifted from the event location, the longer the blend,
        # so the correction never asks for more than ~5 m/s of extra running
        width = max(BLEND_S, float(np.hypot(pos[s0, i, 0] - x, pos[s0, i, 1] - y)) / 5.0)
        for s in range(max(0, int(t - width)), min(T, int(t + width) + 1)):
            if np.isnan(pos[s, i, 0]):
                continue
            w = max(0.0, 1 - abs(s - t) / width)
            pos[s, i] = (1 - w) * pos[s, i] + w * np.array([x, y])


def incident_window(script: dict, eng: dict, trk: dict, block: dict, hz: int = 10):
    """10 Hz positions around one incident, relative time dt = 0 at the incident's key moment."""
    inc = block["incident"]
    period = block["period"]
    t0 = eng["block_anchor"][block["id"]]["t"]
    keys_all = inc["keyframes"]
    kmin = min(k[0][0] for k in keys_all.values())
    kmax = max(k[-1][0] for k in keys_all.values())
    dts = np.round(np.arange(kmin - 2.0, kmax + 1.0 + 1e-9, 1.0 / hz), 2)
    P = trk["periods"][period]
    pos1, secs = P["pos"], P["secs"]
    idx = trk["index"]
    active = active_timeline(script, eng["roster_changes"])(period, t0)
    ents = [p for team in active.values() for p in team] + ["REF", "BALL"] + [e for e in keys_all if e.startswith("AR")]
    positions = {}
    for ent in ents:
        keys = keys_all.get(ent)
        xy = []
        for dt in dts:
            t = t0 + dt
            if ent in idx:
                col = pos1[:, idx[ent]]
                ok = ~np.isnan(col[:, 0])
                base = (float(np.interp(t, secs[ok], col[ok, 0])), float(np.interp(t, secs[ok], col[ok, 1])))
            else:
                base = None
            # inside a keyframed span use the exact spline; outside it the 1 Hz track already
            # carries the blend towards the keyframes, so interpolating it keeps speeds human
            if keys and (base is None or keys[0][0] <= dt <= keys[-1][0]):
                p = catmull_rom(keys, dt)
            else:
                p = base
            xy.append([round(p[0], 2), round(p[1], 2)])
        positions[ent] = xy
    return {"hz": hz, "t0": t0, "dt": [float(d) for d in dts], "positions": positions}
