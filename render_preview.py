"""Quick-look PNGs: each incident at its key moment with the referee's sight line, plus heatmaps.

Usage:  python render_preview.py [data/match_001] [previews]
Not the product UI: just a way to eyeball that the synthetic data tells the story we want.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Arc, Circle, Rectangle  # noqa: E402

D = Path(sys.argv[1] if len(sys.argv) > 1 else "data/match_001")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "previews")
OUT.mkdir(exist_ok=True)
match = json.loads((D / "match.json").read_text())
COL = {code: t["colour"] for code, t in match["teams"].items()}
NUM = {pid: p["number"] for t in match["teams"].values() for pid, p in {**t["starting"], **t["bench"]}.items()}
GRASS, LINE = "#2f6b3a", "#e8efe6"


def draw_pitch(ax, xlim=(0, 105), ylim=(0, 68)):
    ax.set_facecolor(GRASS)
    kw = dict(color=LINE, lw=1.2, zorder=1)
    ax.plot([0, 105, 105, 0, 0], [0, 0, 68, 68, 0], **kw)
    ax.plot([52.5, 52.5], [0, 68], **kw)
    ax.add_patch(Circle((52.5, 34), 9.15, fill=False, ec=LINE, lw=1.2, zorder=1))
    for x0, s in ((0, 1), (105, -1)):
        ax.add_patch(Rectangle((x0, 34 - 20.16), 16.5 * s, 40.32, fill=False, ec=LINE, lw=1.2, zorder=1))
        ax.add_patch(Rectangle((x0, 34 - 9.16), 5.5 * s, 18.32, fill=False, ec=LINE, lw=1.2, zorder=1))
        ax.add_patch(Rectangle((x0, 34 - 3.66), -1.5 * s, 7.32, fill=False, ec=LINE, lw=1.2, zorder=1))
        ax.plot([x0 + 11 * s], [34], "o", color=LINE, ms=2, zorder=1)
        ang = 0 if s == 1 else 180
        ax.add_patch(Arc((x0 + 11 * s, 34), 18.3, 18.3, angle=ang, theta1=-53, theta2=53, color=LINE, lw=1.2, zorder=1))
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])


def frame_at(win, dt):
    i = int(np.argmin([abs(d - dt) for d in win["dt"]]))
    return {e: xy[i] for e, xy in win["positions"].items()}


def incident_panel(ax, inc_id, dt=None, zoom=None):
    inc = json.loads((D / "incidents" / f"{inc_id}.json").read_text())
    an = json.loads((D / "analysis" / f"{inc_id}.json").read_text())
    win = inc["window"]
    dt = inc["focal"]["dt"] if dt is None else dt
    fr = frame_at(win, dt)
    fx, fy = inc["focal"]["x"], inc["focal"]["y"]
    draw_pitch(ax, *(zoom or ((0, 105), (0, 68))))
    involved = {v["id"] for v in inc["involved"].values()}
    for obs in inc["observers"]:
        o = fr[obs]
        blocked = any(abs(f["dt"] - dt) < 0.051 and f["visible_share"] < 0.5 for f in an["observers"][obs]["frames"])
        ax.plot([o[0], fx], [o[1], fy], color="#ff5a4e" if blocked else "#ffe066", lw=2, ls="--" if blocked else "-", zorder=2)
        # 30-degree viewing cone
        v = np.array([fx - o[0], fy - o[1]])
        L = np.linalg.norm(v)
        for a in (-15, 15):
            r = np.radians(a)
            rv = np.array([v[0] * np.cos(r) - v[1] * np.sin(r), v[0] * np.sin(r) + v[1] * np.cos(r)])
            ax.plot([o[0], o[0] + rv[0] * 1.15], [o[1], o[1] + rv[1] * 1.15], color="#ffe066", lw=0.6, alpha=0.6, zorder=2)
        _ = L
    blockers_now = set()
    for obs in inc["observers"]:
        for f in an["observers"][obs]["frames"]:
            if abs(f["dt"] - dt) < 0.051:
                blockers_now.update(f["covering"])
    for ent, (x, y) in fr.items():
        if ent == "BALL":
            continue
        if ent in ("REF",) or ent.startswith("AR"):
            ax.add_patch(Circle((x, y), 0.9, fc="#111111", ec="#ffe066", lw=1.8, zorder=4))
            ax.text(x, y + 1.6, ent, color="#ffe066", ha="center", fontsize=7, zorder=5, weight="bold", clip_on=True)
            continue
        team = ent.split("-")[0]
        ec = "#ffffff" if ent in involved else ("#ff5a4e" if ent in blockers_now else "#0d1a10")
        ax.add_patch(Circle((x, y), 0.9, fc=COL[team], ec=ec, lw=2.0 if ec != "#0d1a10" else 0.6, zorder=4))
        ax.text(x, y, str(NUM.get(ent, "")), color="white", ha="center", va="center", fontsize=6, zorder=5, weight="bold", clip_on=True)
    bx, by = fr["BALL"]
    ax.add_patch(Circle((bx, by), 0.45, fc="white", ec="black", lw=0.8, zorder=6))
    ax.plot([fx], [fy], marker="x", color="white", ms=8, mew=2, zorder=7)
    if inc["type"] == "offside_goal":
        ax.axvline(an["offside"]["offside_line_x"], color="#7fd3ff", lw=1.2, zorder=3)
    o = an["observers"]["REF"]
    sub = f"visible {int(o['visible_share_at_key_moment'] * 100)}% at key moment · {o['distance_to_focal_m']} m away"
    bv = an.get("best_view")
    if bv and not bv["already_good"]:
        bx_, by_ = bv["best"]["position"]
        ax.add_patch(Circle((bx_, by_), 0.9, fc="none", ec="#7CFF9B", lw=1.6, ls="--", zorder=4))
        ax.annotate("", xy=(bx_, by_), xytext=tuple(fr["REF"]), arrowprops=dict(arrowstyle="->", color="#7CFF9B", lw=1.2), zorder=4)
        sub += f"\nbest view: {bv['coaching_note']}"
    if inc["type"] == "offside_goal":
        sub = f"offside by {an['offside']['margin_m'] * 100:.0f} cm · seen at {an['offside']['assistant_referee']['margin_as_seen_deg']}°"
    if inc["type"] == "handball":
        sub = f"referee {an['behind_body']['angle_from_defender_facing_deg']:.0f}° behind the defender"
    if inc["type"] == "dogso":
        sub = f"no covering defender · {an['dogso_cover']['distance_to_goal_m']} m from goal"
    ax.set_title(f"{inc_id} · {inc['clock']} · {inc['type']}\n{sub}", fontsize=9, loc="left")


fig, axes = plt.subplots(2, 3, figsize=(16, 9.6))
zooms = {
    "INC-01": ((0, 40), (14, 54)),
    "INC-02": ((0, 40), (-3, 37)),
    "INC-03": ((70, 105), (0, 40)),
    "INC-04": ((0, 60), (5, 55)),
    "INC-05": ((0, 30), (15, 50)),
}
for ax, (inc_id, z) in zip(axes.flat, zooms.items()):
    incident_panel(ax, inc_id, zoom=z)
ax = axes.flat[-1]
ax.axis("off")
ax.text(0, 0.95, "How to read", fontsize=11, weight="bold", va="top")
ax.text(0, 0.82, "yellow line: official's line of sight to the key point (x)\n"
                 "red dashed: that line is blocked by a body\n"
                 "white ring: players involved in the incident\n"
                 "red ring: players blocking the view\n"
                 "blue line (INC-02): offside line at the pass\n"
                 "green dashed ring: best reachable viewing spot\n\n"
                 "Northbridge in blue, Kestrel in red.\nAll clubs and players are fictional.", fontsize=9, va="top")
fig.suptitle("Referee's Eye: the five incidents at their key moment", fontsize=13, x=0.01, ha="left")
fig.tight_layout()
fig.savefig(OUT / "incidents.png", dpi=110)

# heatmaps
hm = json.loads((D / "heatmaps.json").read_text())
fig, axes = plt.subplots(1, 3, figsize=(16, 4.4))
for ax, (title, grid) in zip(axes, [
    ("Ollie Brandt (NBR 9), attacking left to right", hm["players"]["NBR-09"]),
    ("Yusuf Demir (KES 4), attacking left to right", hm["players"]["KES-04"]),
    ("Referee, second half (pitch frame)", hm["referee"]["2"]),
]):
    g = np.array(grid, dtype=float).T
    draw_pitch(ax)
    ax.imshow(g, extent=(0, 105, 0, 70), origin="lower", cmap="magma", alpha=0.75, zorder=2, interpolation="bicubic")
    ax.set_ylim(0, 68)
    ax.set_title(title, fontsize=9, loc="left")
fig.tight_layout()
fig.savefig(OUT / "heatmaps.png", dpi=110)
print("wrote", OUT / "incidents.png", OUT / "heatmaps.png")
