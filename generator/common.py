"""Shared constants and helpers for the synthetic match generator."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

PITCH_L = 105.0
PITCH_W = 68.0
GOAL_HALF_WIDTH = 3.66
BOX_DEPTH = 16.5
BOX_HALF_WIDTH = 20.16
PERIOD_LEN = 45 * 60  # seconds of regulation time per half

# 4-3-3 slots in the team's own frame: u runs from own goal (0) to opponent goal (105),
# v runs across the pitch. When a team attacks +x, team frame == pitch frame.
SLOTS = {
    "GK": (5.0, 34.0),
    "RB": (33.0, 10.0),
    "RCB": (28.0, 25.0),
    "LCB": (28.0, 43.0),
    "LB": (33.0, 58.0),
    "DM": (42.0, 34.0),
    "RCM": (52.0, 22.0),
    "LCM": (55.0, 46.0),
    "RW": (68.0, 9.0),
    "ST": (73.0, 34.0),
    "LW": (68.0, 59.0),
}

# Restart dead-time ranges (seconds) for routine restarts; not added to added time.
RESTART_DELAY = {
    "throw_in": (7, 13),
    "goal_kick": (14, 24),
    "corner": (24, 36),
    "free_kick": (15, 28),
    "free_kick_attacking": (28, 44),
}


def load_script(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def to_team_frame(x: float, y: float, direction: int) -> tuple[float, float]:
    """Pitch coords -> frame where the team attacks towards u = 105."""
    if direction == 1:
        return x, y
    return PITCH_L - x, PITCH_W - y


def to_pitch_frame(u: float, v: float, direction: int) -> tuple[float, float]:
    if direction == 1:
        return u, v
    return PITCH_L - u, PITCH_W - v


def target_goal(direction: int) -> tuple[float, float]:
    """Centre of the goal a team attacks."""
    return (PITCH_L if direction == 1 else 0.0, PITCH_W / 2)


def clock_label(period: int, t: float) -> str:
    """Broadcast-style clock, e.g. '23:10', '45+02:30', '90+04:30'."""
    base = 0 if period == 1 else 45
    if t <= PERIOD_LEN:
        total = base * 60 + t
        return f"{int(total // 60):02d}:{int(total % 60):02d}"
    extra = t - PERIOD_LEN
    return f"{base + 45}+{int(extra // 60):02d}:{int(extra % 60):02d}"


def shot_value(x: float, y: float, direction: int) -> float:
    """Very simple chance-quality model (probability a shot from here scores).

    Uses distance to goal centre and the angle the goal mouth subtends.
    Calibrated so a central penalty-spot shot is ~0.23 and a 25 m shot is ~0.03.
    """
    gx, gy = target_goal(direction)
    d = math.hypot(gx - x, gy - y)
    a1 = math.atan2(gy - GOAL_HALF_WIDTH - y, gx - x)
    a2 = math.atan2(gy + GOAL_HALF_WIDTH - y, gx - x)
    angle = abs(a2 - a1)
    if angle > math.pi:
        angle = 2 * math.pi - angle
    z = -0.14 * d + 1.0 * angle - 0.3
    return round(1 / (1 + math.exp(-z)), 3)


def in_box(x: float, y: float, defending_goal_x: float) -> bool:
    within_y = abs(y - PITCH_W / 2) <= BOX_HALF_WIDTH
    if defending_goal_x == 0.0:
        return within_y and 0 <= x <= BOX_DEPTH
    return within_y and PITCH_L - BOX_DEPTH <= x <= PITCH_L


def catmull_rom(keys: list[list[float]], t: float) -> tuple[float, float]:
    """Interpolate [[t, x, y], ...] keyframes with a centripetal-free Catmull-Rom spline.

    Clamps outside the keyframe range.
    """
    ts = [k[0] for k in keys]
    if t <= ts[0]:
        return keys[0][1], keys[0][2]
    if t >= ts[-1]:
        return keys[-1][1], keys[-1][2]
    i = max(j for j in range(len(ts) - 1) if ts[j] <= t)
    p0 = np.array(keys[max(i - 1, 0)][1:3])
    p1 = np.array(keys[i][1:3])
    p2 = np.array(keys[i + 1][1:3])
    p3 = np.array(keys[min(i + 2, len(keys) - 1)][1:3])
    t1, t2 = ts[i], ts[i + 1]
    s = (t - t1) / (t2 - t1)
    # tangents scaled to the segment duration so uneven key spacing does not overshoot
    t0 = ts[max(i - 1, 0)]
    t3 = ts[min(i + 2, len(ts) - 1)]
    m1 = (p2 - p0) / max(t2 - t0, 1e-6) * (t2 - t1)
    m2 = (p3 - p1) / max(t3 - t1, 1e-6) * (t2 - t1)
    h00 = 2 * s**3 - 3 * s**2 + 1
    h10 = s**3 - 2 * s**2 + s
    h01 = -2 * s**3 + 3 * s**2
    h11 = s**3 - s**2
    p = h00 * p1 + h10 * m1 + h01 * p2 + h11 * m2
    return float(p[0]), float(p[1])


def r2(v: float) -> float:
    return round(float(v), 2)
