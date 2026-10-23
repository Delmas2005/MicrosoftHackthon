"""Heatmap grids and referee coverage from the 1 Hz track."""
from __future__ import annotations

import math

import numpy as np

from .common import PITCH_L, PITCH_W

CELL = 5.0
NX = int(math.ceil(PITCH_L / CELL))   # 21
NY = int(math.ceil(PITCH_W / CELL))   # 14


def _bin(x, y):
    ix = int(min(max(x, 0.0), PITCH_L - 1e-6) // CELL)
    iy = int(min(max(y, 0.0), PITCH_W - 1e-6) // CELL)
    return ix, iy


def build_heatmaps(script: dict, trk: dict) -> dict:
    """Seconds spent in each 5 m cell while the ball was in play.

    Players: in the team's attacking frame (always attacking left to right, x = 105 is the
    opponent's goal), so both halves add up. Referee: pitch frame, one grid per half,
    because the referee's diagonal does not flip with the teams.
    """
    idx = trk["index"]
    players = {}
    ref = {}
    for period, P in trk["periods"].items():
        pos, dead = P["pos"], P["dead"]
        dirs = script["attack_direction"][str(period)]
        live = np.where(~dead)[0]
        for ent, i in idx.items():
            if ent == "BALL":
                continue
            if ent == "REF":
                g = np.zeros((NX, NY), dtype=int)
                for s in live:
                    x, y = pos[s, i]
                    if not np.isnan(x):
                        ix, iy = _bin(x, y)
                        g[ix, iy] += 1
                ref[str(period)] = g.tolist()
                continue
            team = ent.split("-")[0]
            g = np.array(players.get(ent, np.zeros((NX, NY), dtype=int)))
            for s in live:
                x, y = pos[s, i]
                if np.isnan(x):
                    continue
                if dirs[team] == -1:
                    x, y = PITCH_L - x, PITCH_W - y
                ix, iy = _bin(x, y)
                g[ix, iy] += 1
            players[ent] = g
    players = {k: v.tolist() for k, v in players.items() if int(np.sum(v)) > 0}
    return {
        "grid": {"cell_m": CELL, "nx": NX, "ny": NY, "index": "cells[ix][iy]; ix along length, iy along width",
                 "unit": "seconds with the ball in play"},
        "players_frame": "team attacking frame: x = 105 is always the opponent's goal",
        "referee_frame": "pitch frame, per half",
        "players": players,
        "referee": ref,
    }


def incident_occupancy(window: dict, until_dt: float = 0.0) -> dict:
    """Who occupied which 5 m cells in the run-up to the key moment (pitch frame)."""
    out = {}
    for ent, xy in window["positions"].items():
        if ent in ("BALL", "REF") or ent.startswith("AR"):
            continue
        team = ent.split("-")[0]
        g = out.setdefault(team, np.zeros((NX, NY), dtype=float))
        for dt, (x, y) in zip(window["dt"], xy):
            if dt > until_dt:
                break
            ix, iy = _bin(x, y)
            g[ix, iy] += 1.0 / window["hz"]
    return {"cell_m": CELL, "unit": "player-seconds before the key moment", "teams": {k: np.round(v, 1).tolist() for k, v in out.items()}}


def referee_coverage(trk: dict, period: int, t: float, focal_xy, radius: float = 15.0) -> dict:
    """How the referee's position at the incident compares with his own habits."""
    P = trk["periods"][period]
    pos, dead = P["pos"], P["dead"]
    ri, bi = trk["index"]["REF"], trk["index"]["BALL"]
    live = ~dead
    d_ball = np.linalg.norm(pos[live, ri] - pos[live, bi], axis=1)
    ball_near = np.linalg.norm(pos[:, bi] - np.array(focal_xy), axis=1) < radius
    # sustained play in this area: the ball has been near the spot for the last 3 seconds
    sustained = ball_near.copy()
    for k in range(1, 4):
        sustained[k:] &= ball_near[:-k]
        sustained[:k] = False
    near = live & sustained
    usual = np.median(pos[near, ri], axis=0) if near.any() else None
    s = int(round(t))
    ref_now = pos[min(s, len(pos) - 1), ri]
    dist_focal = float(np.linalg.norm(ref_now - np.array(focal_xy)))
    res = {
        "median_distance_to_ball_m": round(float(np.median(d_ball)), 1),
        "p90_distance_to_ball_m": round(float(np.percentile(d_ball, 90)), 1),
        "distance_to_incident_m": round(dist_focal, 1),
        "percentile_vs_half": int(round(float((d_ball < dist_focal).mean()) * 100)),
    }
    if usual is not None:
        res["usual_position_for_play_here"] = [round(float(usual[0]), 1), round(float(usual[1]), 1)]
        res["offset_from_usual_m"] = round(float(np.linalg.norm(ref_now - usual)), 1)
        res["seconds_of_play_in_this_area"] = int(near.sum())
    return res
