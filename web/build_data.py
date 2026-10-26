"""Bundle what the screens need into one file: web/data/bundle.json.

    py web/build_data.py
    py web/build_data.py --cards examples/cards_agents_match_001.jsonl

The screens are a static site (Azure Static Web Apps): they read this bundle and replay the cards.
Only what a viewer may see goes in: positions, the Referee's Eye measurements and the published
cards. The answer files (ground_truth/) never leave the repo.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def clock_seconds(clock: str) -> float:
    """'38:40' -> 2320, '90+04:30' -> 5670 (added time after 90 minutes)."""
    base, _, extra = clock.partition("+")
    if extra:
        m, s = extra.split(":")
        return int(base) * 60 + int(m) * 60 + int(s)
    m, s = base.split(":")
    return int(m) * 60 + int(s)


def observer_view(o: dict) -> dict:
    keep = ("position_at_key_moment", "distance_to_focal_m", "visible_share_at_key_moment",
            "mean_visible_share_in_critical_window", "blockers", "blocker_mean_cover", "critical_window_dt")
    out = {k: o[k] for k in keep if k in o}
    out["frames"] = [{"dt": f["dt"], "visible": f["visible_share"], "covering": f.get("covering", {})}
                     for f in o.get("frames", [])]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="data/match_001")
    ap.add_argument("--cards", default="examples/cards_agents_match_001.jsonl",
                    help="published cards to replay (falls back to the template cards if missing)")
    ap.add_argument("--out", default="web/data/bundle.json")
    args = ap.parse_args()

    mdir = ROOT / args.match
    match = json.loads((mdir / "match.json").read_text(encoding="utf-8"))
    teams = {code: {"name": t["name"], "short": t["short"], "colour": t["colour"]} for code, t in match["teams"].items()}
    players = {pid: {"name": p["name"], "number": p["number"], "team": code}
               for code, t in match["teams"].items() for pid, p in {**t["starting"], **t["bench"]}.items()}

    incidents = []
    for meta in match["incidents"]:
        iid = meta["incident_id"]
        inc = json.loads((mdir / "incidents" / f"{iid}.json").read_text(encoding="utf-8"))
        ana = json.loads((mdir / "analysis" / f"{iid}.json").read_text(encoding="utf-8"))
        at = clock_seconds(inc["clock"])
        score = {code: sum(1 for g in match["goals"] if g["team"] == code and clock_seconds(g["clock"]) < at)
                 for code in teams}
        extra = {}
        if "offside" in ana:
            o = ana["offside"]
            extra["offside"] = {"line_x": o["offside_line_x"], "attacker_x": o["attacker_x"], "margin_m": o["margin_m"],
                                "second_last_defender": o["second_last_defender"]}
        incidents.append({
            "id": iid, "clock": inc["clock"], "period": inc["period"], "t": inc["t"], "type": inc["type"],
            "headline": inc["headline"], "attacking_team": inc["attacking_team"], "defending_team": inc["defending_team"],
            "involved": {role: p["id"] for role, p in inc["involved"].items()},
            "focal": inc["focal"], "critical_window": inc["critical_window"],
            "observers": {oid: {"name": name, **observer_view(ana["observers"][oid])} for oid, name in inc["observers"].items()},
            "best_view": {k: ana["best_view"][k] for k in ("actual", "best", "move_m", "already_good")},
            "window": inc["window"], "score_before": score, **extra,
        })

    cards_path = ROOT / args.cards
    if not cards_path.exists():
        cards_path = ROOT / "examples" / "cards_match_001.jsonl"
    cards = [json.loads(l) for l in cards_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for c in cards:
        c.setdefault("revision", 1)
    from build_audio import attach  # the Azure AI Speech recordings, if web/build_audio.py has made them
    voiced = attach(cards)

    bundle = {"match": {"venue": match["venue"], "teams": teams, "officials": match["officials"],
                        "pitch": match["pitch"], "disclaimer": match["disclaimer"]},
              "players": players, "incidents": incidents, "cards": cards,
              "cards_source": cards_path.relative_to(ROOT).as_posix()}
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{out.relative_to(ROOT)}: {len(incidents)} incidents, {len(cards)} cards from {bundle['cards_source']}, "
          f"{out.stat().st_size // 1024} KB, {voiced} audio cards with a recording")


if __name__ == "__main__":
    main()
