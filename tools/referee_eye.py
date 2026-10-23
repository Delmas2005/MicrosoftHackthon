"""Referee's Eye: deterministic geometry over an incident's 10 Hz positions.

These are the tools the Perception and Attack agents call. They never guess: every number
comes from positions in the incident window, so every sentence an agent writes can cite it.

All functions take the `window` dict produced by generator.tracking.incident_window:
    {"hz": 10, "t0": <period seconds>, "dt": [...], "positions": {entity: [[x, y], ...]}}
"""
from __future__ import annotations

import math

import zlib

PITCH_L, PITCH_W = 105.0, 68.0
BODY_RADIUS = 0.35      # metres: half the width of a player's body seen from any side
EYE_HEIGHT = 1.70       # metres: an official's eye line
TARGET_WIDTH = 0.5      # metres: width of the thing an official must see (a contact, an arm, a grip)
ATTACKER_SPEED = 8.0    # m/s, used for covering-defender reach times
DEFENDER_SPEED = 8.5
REACTION_S = 0.3
REF_VMAX = 7.0          # m/s, referee running speed used for the best-view search


def player_height(entity: str) -> float:
    """Deterministic synthetic height, 1.72 to 1.95 m (keepers +0.08 m)."""
    h = 1.72 + (zlib.crc32(entity.encode()) % 24) / 100
    return round(h + (0.08 if entity.endswith("-01") else 0.0), 2)


def _frame(window, dt: float) -> int:
    dts = window["dt"]
    return min(range(len(dts)), key=lambda i: abs(dts[i] - dt))


def at(window, entity: str, dt: float):
    xy = window["positions"].get(entity)
    if xy is None:
        return None
    return tuple(xy[_frame(window, dt)])


def velocity(window, entity: str, dt: float):
    i = _frame(window, dt)
    xy = window["positions"][entity]
    j0, j1 = max(i - 2, 0), min(i + 2, len(xy) - 1)
    span = window["dt"][j1] - window["dt"][j0]
    if span <= 0:
        return (0.0, 0.0)
    return ((xy[j1][0] - xy[j0][0]) / span, (xy[j1][1] - xy[j0][1]) / span)


def _seg_dist(p, a, b):
    """Distance from point p to segment a-b, and the projection parameter along it."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay), 0.0
    s = ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2
    s_c = min(max(s, 0.0), 1.0)
    cx, cy = ax + s_c * dx, ay + s_c * dy
    return math.hypot(p[0] - cx, p[1] - cy), s


def _angle_between(v1, v2):
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return None
    c = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _wrap(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


def visibility_at(obs_xy, focal_xy, occluders, target_height=0.5, target_width=TARGET_WIDTH, eye=EYE_HEIGHT):
    """Share of the target an official can see from obs_xy (0 = fully hidden, 1 = clear).

    2.5D model: in plan, every body is a 0.35 m-radius disc and the target is a segment
    `target_width` wide, square to the line of sight; the covered share is the union of the
    angular intervals the discs hide. In elevation, a body only blocks if the sight line from
    the official's eye down (or up) to the target passes below the top of that body.
    occluders: iterable of (entity, (x, y), height_m).
    """
    dx, dy = focal_xy[0] - obs_xy[0], focal_xy[1] - obs_xy[1]
    d_t = math.hypot(dx, dy)
    if d_t < 0.5:
        return 1.0, {}
    th_t = math.atan2(dy, dx)
    alpha = math.atan((target_width / 2) / d_t)
    intervals, per = [], {}
    for ent, (ox, oy), h in occluders:
        ex, ey = ox - obs_xy[0], oy - obs_xy[1]
        d_o = math.hypot(ex, ey)
        if d_o < 0.6 or d_o > d_t - 0.3:
            continue
        delta = _wrap(math.atan2(ey, ex) - th_t)
        beta = math.asin(min(1.0, BODY_RADIUS / d_o))
        if abs(delta) > alpha + beta:
            continue
        line_h = eye + (target_height - eye) * (d_o / d_t)
        if line_h > h:
            continue  # the sight line passes over this player's head
        lo, hi = max(-alpha, delta - beta), min(alpha, delta + beta)
        if hi > lo:
            intervals.append((lo, hi))
            per[ent] = round((hi - lo) / (2 * alpha), 2)
    intervals.sort()
    covered, cur_lo, cur_hi = 0.0, None, None
    for lo, hi in intervals:
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                covered += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None:
        covered += cur_hi - cur_lo
    return round(max(0.0, 1 - covered / (2 * alpha)), 2), per


def _occluders(window, i, exclude, heights=None):
    out = []
    for ent, xy in window["positions"].items():
        if ent in exclude or ent == "BALL" or ent == "REF" or ent.startswith("AR"):
            continue
        h = (heights or {}).get(ent, player_height(ent))
        out.append((ent, tuple(xy[i]), h))
    return out


def visibility_series(window, observer: str, focal_xy, exclude=(), target_height=0.5, heights=None):
    """Per frame: how much of the target the observer could see, and who hid it."""
    frames = []
    for i, dt in enumerate(window["dt"]):
        o = window["positions"][observer][i]
        vis, per = visibility_at(o, focal_xy, _occluders(window, i, set(exclude) | {observer}, heights), target_height)
        frames.append({"dt": dt, "visible_share": vis, "covering": per,
                       "distance_m": round(math.hypot(focal_xy[0] - o[0], focal_xy[1] - o[1]), 2)})
    return frames


def summarize_visibility(frames, critical_window, key_dt=0.0, hz=10):
    lo, hi = critical_window
    crit = [f for f in frames if lo - 1e-9 <= f["dt"] <= hi + 1e-9]
    key = min(frames, key=lambda f: abs(f["dt"] - key_dt))
    shares = [f["visible_share"] for f in crit] or [key["visible_share"]]
    cover = {}
    for f in crit:
        for e, c in f["covering"].items():
            cover[e] = cover.get(e, 0.0) + c / max(len(crit), 1)
    best = run = 0
    span, start = None, None
    for f in frames:
        if f["visible_share"] < 0.5:
            run += 1
            start = f["dt"] if run == 1 else start
            if run > best:
                best, span = run, [start, f["dt"]]
        else:
            run = 0
    return {
        "critical_window_dt": critical_window,
        "visible_share_at_key_moment": key["visible_share"],
        "mean_visible_share_in_critical_window": round(sum(shares) / len(shares), 2),
        "min_visible_share_in_critical_window": min(shares),
        "seconds_mostly_hidden_in_critical_window": round(sum(1 for x in shares if x < 0.5) / hz, 1),
        "blockers": [e for e, _ in sorted(cover.items(), key=lambda z: -z[1])],
        "blocker_mean_cover": {e: round(c, 2) for e, c in sorted(cover.items(), key=lambda z: -z[1])},
        "longest_mostly_hidden_run_s": round(best / hz, 1),
        "longest_mostly_hidden_run_dt": span,
    }


def best_view(window, observer: str, focal_xy, exclude=(), mover: str | None = None, dt: float = 0.0,
              target_height=0.5, lead_s=1.5, heights=None, facing=None):
    """Where could the official have stood, given where he was `lead_s` earlier, to see it best?

    Searches a 1 m grid inside the circle he could reach at REF_VMAX. Score =
    visibility x (0.6 + 0.4 x side-on factor) x distance factor (10 to 20 m is ideal).
    `facing` = (defender_xy, facing_xy) for contacts on the front of a body (handball): standing
    behind the defender hides the contact behind his own body, so those spots score lower.
    Returned as a coaching note: how many metres to his left/right and closer/further.
    """
    i = _frame(window, dt)
    i0 = _frame(window, dt - lead_s)
    p_now = tuple(window["positions"][observer][i])
    p0 = tuple(window["positions"][observer][i0])
    reach = REF_VMAX * (window["dt"][i] - window["dt"][i0])
    occ = _occluders(window, i, set(exclude) | {observer}, heights)
    run = velocity(window, mover, dt) if mover else None
    bodies = [xy for _, xy, _ in occ] + [tuple(window["positions"][e][i]) for e in exclude if e in window["positions"] and e != "BALL"]

    def score(c):
        d = math.hypot(focal_xy[0] - c[0], focal_xy[1] - c[1])
        if d < 6 or d > 30:
            return None
        vis, _ = visibility_at(c, focal_xy, occ, target_height)
        side = 1.0
        if run and math.hypot(*run) > 0.5:
            a = _angle_between((focal_xy[0] - c[0], focal_xy[1] - c[1]), run)
            side = math.sin(math.radians(a))
        df = 1.0 if 10 <= d <= 20 else max(0.6, 1 - (10 - d) * 0.1 if d < 10 else 1 - (d - 20) * 0.04)
        bf = 1.0
        if facing:
            (dx_, dy_), (tx_, ty_) = facing
            ang = _angle_between((tx_ - dx_, ty_ - dy_), (c[0] - dx_, c[1] - dy_)) or 0.0
            bf = 1.0 if ang <= 90 else max(0.2, 1 - (ang - 90) / 60 * 0.8)
            vis = vis * bf
        return vis * (0.6 + 0.4 * side) * df, vis, side, d

    base = score(p_now)
    best = (base, p_now) if base else (None, p_now)
    r = int(reach) + 1
    for gx in range(-r, r + 1):
        for gy in range(-r, r + 1):
            c = (round(p0[0] + gx, 1), round(p0[1] + gy, 1))
            if math.hypot(gx, gy) > reach or not (0 <= c[0] <= PITCH_L and 0 <= c[1] <= PITCH_W):
                continue
            if any(math.hypot(c[0] - b[0], c[1] - b[1]) < 1.0 for b in bodies):
                continue
            sc = score(c)
            if sc and (best[0] is None or sc[0] > best[0][0] + 1e-6 or
                       (abs(sc[0] - best[0][0]) < 1e-6 and math.hypot(c[0] - p_now[0], c[1] - p_now[1]) < math.hypot(best[1][0] - p_now[0], best[1][1] - p_now[1]))):
                best = (sc, c)
    (sc_best, vis_b, side_b, d_b), c_best = best
    fx, fy = focal_xy[0] - p_now[0], focal_xy[1] - p_now[1]
    fn = math.hypot(fx, fy)
    fwd = ((c_best[0] - p_now[0]) * fx + (c_best[1] - p_now[1]) * fy) / fn
    left = ((c_best[0] - p_now[0]) * -fy + (c_best[1] - p_now[1]) * fx) / fn
    res = {
        "actual": {"position": [round(p_now[0], 1), round(p_now[1], 1)],
                   "visible_share": round(base[1], 2) if base else None, "score": round(base[0], 2) if base else None},
        "best": {"position": [round(c_best[0], 1), round(c_best[1], 1)], "visible_share": vis_b,
                 "side_on_factor": round(side_b, 2), "distance_to_focal_m": round(d_b, 1), "score": round(sc_best, 2)},
        "move_m": {"left": round(left, 1), "closer": round(fwd, 1), "total": round(math.hypot(left, fwd), 1)},
        "reachable_radius_m": round(reach, 1),
        "already_good": bool(base and base[1] >= 0.9 and sc_best - base[0] < 0.1),
    }
    side_now = base[2] if base else 0.0
    res["actual"]["side_on_deg"] = round(math.degrees(math.asin(min(1.0, side_now))), 0)
    res["best"]["side_on_deg"] = round(math.degrees(math.asin(min(1.0, side_b))), 0)
    lr = "left" if left > 0 else "right"
    fb = "closer" if fwd > 0 else "further back"
    parts = []
    if abs(left) >= 1:
        parts.append(f"{abs(left):.0f} m to his {lr}")
    if abs(fwd) >= 1:
        parts.append(f"{abs(fwd):.0f} m {fb}")
    v0, v1 = int(round((base[1] if base else 0) * 100)), int(round(vis_b * 100))
    gain = f"visibility {v0}% -> {v1}%" if v1 - v0 >= 5 else \
        f"side-on angle {res['actual']['side_on_deg']:.0f} -> {res['best']['side_on_deg']:.0f} degrees"
    res["coaching_note"] = ("Position already gave a clear view." if res["already_good"] or not parts else
                            f"{' and '.join(parts)}: {gain}.")
    return res


def view_angle(window, observer: str, focal_xy, mover: str, dt: float = 0.0):
    """Angle between the observer's line of sight and the direction the key player is moving.

    Near 0 or 180 degrees: looking straight along the run, so depth (who is ahead of whom,
    how close a covering player is) is hard to judge. Near 90 degrees: side-on, the best view.
    """
    o = at(window, observer, dt)
    v = velocity(window, mover, dt)
    los = (focal_xy[0] - o[0], focal_xy[1] - o[1])
    a = _angle_between(los, v)
    if a is None:
        return None
    side_on = 90 - abs(90 - a)
    return {"angle_to_run_deg": round(a, 1), "side_on_score_deg": round(side_on, 1),
            "depth_hard_to_judge": side_on < 30}


def behind_body(window, observer: str, defender: str, facing_xy, dt: float = 0.0):
    """Is the observer looking at the defender's back (contact on the far side of the body)?"""
    d = at(window, defender, dt)
    o = at(window, observer, dt)
    facing = (facing_xy[0] - d[0], facing_xy[1] - d[1])
    to_obs = (o[0] - d[0], o[1] - d[1])
    a = _angle_between(facing, to_obs)
    return {"angle_from_defender_facing_deg": round(a, 1), "observer_behind_defender": a > 110}


def offside(window, attacker: str, defending_ids, attack_dir: int, dt: float = 0.0, ar: str | None = None):
    """Offside geometry at the moment of the pass (dt)."""
    atk = at(window, attacker, dt)
    ball = at(window, "BALL", dt)
    # defenders ordered from the goal line the attacker is attacking: [0] is usually the keeper
    goal_x = PITCH_L if attack_dir == 1 else 0.0
    defs = sorted(((d, at(window, d, dt)) for d in defending_ids if window["positions"].get(d)),
                  key=lambda z: abs(goal_x - z[1][0]))
    second_last = defs[1]
    line_x = second_last[1][0]
    margin = (atk[0] - line_x) * attack_dir
    ball_line = (atk[0] - ball[0]) * attack_dir
    v = velocity(window, attacker, dt)
    speed = math.hypot(*v)
    res = {
        "second_last_defender": second_last[0],
        "offside_line_x": round(line_x, 2),
        "attacker_x": round(atk[0], 2),
        "margin_m": round(margin, 2),
        "offside_position": margin > 0 and ball_line > 0,
        "attacker_speed_m_s": round(speed, 1),
        "attacker_moves_per_frame_m": {"10fps": round(speed / 10, 2), "50fps": round(speed / 50, 2)},
    }
    if ar:
        a = at(window, ar, dt)
        dist = math.hypot(atk[0] - a[0], atk[1] - a[1])
        res["assistant_referee"] = {
            "id": ar,
            # positive = the assistant stood upfield of the offside line (towards halfway)
            "alignment_offset_m": round((a[0] - line_x) * -attack_dir, 2),
            "distance_to_attacker_m": round(dist, 1),
            "margin_as_seen_deg": round(math.degrees(math.atan2(abs(margin), dist)), 2),
        }
    return res


def dogso_cover(window, attacker: str, offender: str, defending_ids, attack_dir: int, dt: float = 0.0, goalkeepers=()):
    """Could any covering outfield defender have reached the attacker's path to goal in time?

    The goalkeeper is reported separately: a keeper in goal does not stop an obvious
    goal-scoring opportunity on its own.
    """
    a = at(window, attacker, dt)
    goal = (PITCH_L if attack_dir == 1 else 0.0, PITCH_W / 2)
    L = math.hypot(goal[0] - a[0], goal[1] - a[1])
    ux, uy = (goal[0] - a[0]) / L, (goal[1] - a[1]) / L
    path = [(a[0] + ux * s, a[1] + uy * s) for s in [i * 0.5 for i in range(int((L - 6.0) / 0.5) + 1)]]
    out = []
    for d in defending_ids:
        if d == offender or d in goalkeepers or window["positions"].get(d) is None:
            continue
        p = at(window, d, dt)
        best = None
        for k, q in enumerate(path):
            t_att = k * 0.5 / ATTACKER_SPEED
            t_def = math.hypot(q[0] - p[0], q[1] - p[1]) / DEFENDER_SPEED + REACTION_S
            m = t_att - t_def
            if best is None or m > best[0]:
                best = (m, q, t_att)
        out.append({"defender": d, "best_margin_s": round(best[0], 2), "can_cover": best[0] >= 0,
                    "distance_now_m": round(math.hypot(p[0] - a[0], p[1] - a[1]), 1)})
    out.sort(key=lambda z: -z["best_margin_s"])
    gk = next((g for g in goalkeepers if window["positions"].get(g)), None)
    keeper = None
    if gk:
        g = at(window, gk, dt)
        keeper = {"id": gk, "distance_to_attacker_m": round(math.hypot(g[0] - a[0], g[1] - a[1]), 1),
                  "off_goal_line_m": round(abs(g[0] - goal[0]), 1)}
    return {"distance_to_goal_m": round(L, 1),
            "direction_to_goal_deg": round(math.degrees(math.atan2(abs(goal[1] - a[1]), abs(goal[0] - a[0]))), 1),
            "covering": [c for c in out if c["can_cover"]],
            "closest_attempt": out[0] if out else None, "any_cover": any(c["can_cover"] for c in out),
            "goalkeeper": keeper}


def shot_value(x, y, attack_dir):
    gx, gy = (PITCH_L if attack_dir == 1 else 0.0), PITCH_W / 2
    d = math.hypot(gx - x, gy - y)
    a1 = math.atan2(gy - 3.66 - y, gx - x)
    a2 = math.atan2(gy + 3.66 - y, gx - x)
    ang = abs(a2 - a1)
    if ang > math.pi:
        ang = 2 * math.pi - ang
    z = -0.14 * d + 1.0 * ang - 0.3
    return round(1 / (1 + math.exp(-z)), 3)


def attack_context(window, attacking_ids, defending_ids, attack_dir: int, dt: float = 0.0, carrier: str | None = None,
                   goalkeepers=(), project_ball_from_dt: float | None = None, beaten=()):
    """What the attack was worth at the key moment: space, numbers and chance value.

    * chance_value_from_here: a shot from the carrier's spot right now.
    * chance_value_if_unchallenged: only when no outfield defender is goal side; the value of
      a finish from 11 m out on the carrier's line to goal.
    * projected_ball_destination: for a blocked cross, where the ball was heading one second on.
    """
    ball = at(window, "BALL", dt)
    goal_x = PITCH_L if attack_dir == 1 else 0.0
    ref_pt = at(window, carrier, dt) if carrier else ball

    goal_c = (goal_x, PITCH_W / 2)
    carrier_to_goal = math.hypot(goal_c[0] - ref_pt[0], goal_c[1] - ref_pt[1])

    def goal_side(xy):
        # closer to the goal centre than the carrier is
        return math.hypot(goal_c[0] - xy[0], goal_c[1] - xy[1]) < carrier_to_goal + 0.5

    def near(xy):
        return math.hypot(xy[0] - ref_pt[0], xy[1] - ref_pt[1]) < 25.0

    att = [a for a in attacking_ids if window["positions"].get(a) and goal_side(at(window, a, dt)) and near(at(window, a, dt))]
    dfn = [d for d in defending_ids if window["positions"].get(d) and goal_side(at(window, d, dt)) and near(at(window, d, dt))]
    # `beaten`: defenders the attacker has already gone past (e.g. the player who commits the foul)
    outfield_dfn = [d for d in dfn if d not in goalkeepers and d not in beaten]
    dist_goal = math.hypot(goal_x - ref_pt[0], PITCH_W / 2 - ref_pt[1])
    res = {
        "ball": [round(ball[0], 1), round(ball[1], 1)],
        "carrier": carrier,
        "distance_to_goal_m": round(dist_goal, 1),
        "chance_value_from_here": shot_value(ref_pt[0], ref_pt[1], attack_dir),
        "attackers_in_play": len(att),
        "outfield_defenders_goal_side": len(outfield_dfn),
        "numbers": f"{len(att)} v {len(outfield_dfn)}",
        "goal_side_defenders": outfield_dfn,
        "ignored_as_beaten": list(beaten),
        "goalkeeper_only_behind": len(outfield_dfn) == 0,
    }
    if res["goalkeeper_only_behind"] and dist_goal > 11.0:
        f = (dist_goal - 11.0) / dist_goal
        px, py = ref_pt[0] + (goal_x - ref_pt[0]) * f, ref_pt[1] + (PITCH_W / 2 - ref_pt[1]) * f
        res["chance_value_if_unchallenged"] = shot_value(px, py, attack_dir)
    if project_ball_from_dt is not None:
        b0 = at(window, "BALL", project_ball_from_dt)
        vx, vy = (ball[0] - b0[0]) / max(dt - project_ball_from_dt, 1e-6), (ball[1] - b0[1]) / max(dt - project_ball_from_dt, 1e-6)
        dest = (ball[0] + vx * 1.0, ball[1] + vy * 1.0)
        res["projected_ball_destination"] = [round(dest[0], 1), round(dest[1], 1)]
        res["chance_value_at_destination"] = shot_value(dest[0], dest[1], attack_dir)
    return res


def analyze_incident(script: dict, block: dict, window: dict) -> dict:
    """Run every Referee's Eye tool that applies to this incident type."""
    inc = block["incident"]
    period = str(block["period"])
    atk_team, def_team = inc["attacking_team"], inc["defending_team"]
    attack_dir = script["attack_direction"][period][atk_team]
    focal = (inc["focal"]["x"], inc["focal"]["y"])
    fdt = inc["focal"]["dt"]
    ids = window["positions"].keys()
    attacking_ids = [e for e in ids if e.startswith(atk_team)]
    defending_ids = [e for e in ids if e.startswith(def_team)]
    involved = list(inc["involved"].values())
    goalkeepers = {pid for t in script["teams"].values() for pid, p in t["starting"].items() if p["slot"] == "GK"}
    out = {"incident_id": block["id"], "type": inc["type"], "observers": {}}
    mover = inc["involved"].get("attacker")
    th = inc["focal"].get("height_m", 0.5)
    out["target"] = {"height_m": th, "width_m": TARGET_WIDTH, "what": inc["focal"]["what"]}
    for obs in inc["observers"]:
        frames = visibility_series(window, obs, focal, exclude=involved, target_height=th)
        summary = summarize_visibility(frames, inc["critical_window"], fdt, window["hz"])
        o = at(window, obs, fdt)
        summary["position_at_key_moment"] = [round(o[0], 1), round(o[1], 1)]
        summary["distance_to_focal_m"] = round(math.hypot(focal[0] - o[0], focal[1] - o[1]), 1)
        if mover:
            summary["view"] = view_angle(window, obs, focal, mover, fdt)
        summary["frames"] = [{"dt": f["dt"], "visible_share": f["visible_share"], "covering": f["covering"]}
                             for f in frames if f["visible_share"] < 0.99]
        out["observers"][obs] = summary
    facing = None
    if inc.get("facing_target"):
        facing = (at(window, inc["involved"]["defender"], fdt), (inc["facing_target"]["x"], inc["facing_target"]["y"]))
    out["best_view"] = best_view(window, "REF", focal, exclude=involved, mover=mover, dt=fdt, target_height=th, facing=facing)
    if inc.get("facing_target"):
        ft = inc["facing_target"]
        out["behind_body"] = behind_body(window, "REF", inc["involved"]["defender"], (ft["x"], ft["y"]), fdt)
    if inc["type"] == "offside_goal":
        ar = next((o for o in inc["observers"] if o.startswith("AR")), None)
        out["offside"] = offside(window, inc["involved"]["attacker"], defending_ids, attack_dir, fdt, ar)
    if inc["type"] == "dogso":
        out["dogso_cover"] = dogso_cover(window, inc["involved"]["attacker"], inc["involved"]["offender"],
                                         defending_ids, attack_dir, fdt, goalkeepers & set(defending_ids))
    project = fdt - 0.3 if inc["type"] == "handball" else None
    carrier = None if inc["type"] in ("handball", "penalty_claim_holding") else mover
    out["attack"] = attack_context(window, attacking_ids, defending_ids, attack_dir, fdt, carrier=carrier,
                                   goalkeepers=goalkeepers & set(defending_ids), project_ball_from_dt=project,
                                   beaten=[inc["involved"]["offender"]] if inc["type"] == "dogso" else [])
    return out


def analyze_from_files(match: dict, incident: dict) -> dict:
    """Recompute the analysis from what is published: match.json + incidents/<id>.json.

    Used by the MCP server and by the verifier, so no one has to trust a cached analysis file.
    """
    script_like = {
        "attack_direction": match["attack_direction"],
        "teams": {code: {"starting": t["starting"]} for code, t in match["teams"].items()},
    }
    block_like = {
        "id": incident["incident_id"],
        "period": incident["period"],
        "incident": {
            "type": incident["type"],
            "attacking_team": incident["attacking_team"],
            "defending_team": incident["defending_team"],
            "involved": {role: v["id"] for role, v in incident["involved"].items()},
            "focal": incident["focal"],
            "critical_window": incident["critical_window"],
            "observers": list(incident["observers"]),
        },
    }
    if "facing_target" in incident:
        block_like["incident"]["facing_target"] = incident["facing_target"]
    return analyze_incident(script_like, block_like, incident["window"])
