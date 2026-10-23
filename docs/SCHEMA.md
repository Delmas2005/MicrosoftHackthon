# Fourth Official: synthetic match data format

This is the contract between the **generator track** (who makes the data) and the **agents track**
(who reads it). If you change a field, change it here first and tell the other person.

Everything is synthetic. Clubs, players, officials and the venue are invented.

## Run it

```bash
python generate.py      # writes data/match_001/ (about 6 seconds, needs numpy)
python validate.py      # 28 sanity checks, exits 1 if any fail
python render_preview.py   # optional: previews/incidents.png and previews/heatmaps.png (needs matplotlib)
```

`generate.py --seed N` gives a different random match around the same scripted incidents.
Same seed, same match, every time.

## Coordinates and time

| Thing | Convention |
|---|---|
| Pitch | 105 m × 68 m. `x` runs along the length 0 → 105, `y` across 0 → 68. Origin at a corner flag. |
| Goals | Centred at (0, 34) and (105, 34). Goal mouth is 7.32 m wide. |
| Attack direction | `match.json → attack_direction`. Half 1: Northbridge attack +x (towards x = 105), Kestrel attack −x. Teams swap at half-time. |
| Time `t` | Seconds since the **start of that half**. Every record also has `period` (1 or 2). |
| `clock` | Broadcast label: `23:10`, `45+02:30`, `90+04:30`. |
| Player ids | `NBR-09`, `KES-04` (team code + shirt number). Officials: `REF`, `AR1`. The ball is `BALL`. |

## Files in `data/match_001/`

| File | What it is | Who reads it |
|---|---|---|
| `match.json` | Teams, line-ups, officials, final score, goals, halves, subs and red cards, incident index | Everyone |
| `events.jsonl` | The **live feed**: one event per line, in time order | Ingest / Incident Spotter |
| `stoppages.json` | Every dead-ball interval and the added-time breakdown | Timekeeper |
| `tracking_1hz.csv` | Positions of all players, the referee and the ball, once per second | Heatmaps, momentum |
| `incidents/INC-0X.json` | What a live system would know about an incident, plus 10 Hz positions | Protocol, Laws Clerk, Perception, Attack |
| `analysis/INC-0X.json` | Output of the Referee's Eye tools for that incident | Perception, Attack, front end |
| `ground_truth/INC-0X.json` | The correct decision and a panel-style note | **Evaluation only, never give to agents** |
| `heatmaps.json` | 5 m grids for every player and the referee | Front end, phone view |

### Why ground truth is a separate folder

The agents must reach their explanation from the events, the positions and the Laws, the way a
real system would. We then score them against `ground_truth/` (for example as a Foundry
evaluation). That gives us an accuracy number to show the judges.

## `events.jsonl`

One JSON object per line. Fields present on most events:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | string | `E00001`, unique, increasing |
| `period`, `t`, `clock` | int, float, string | When it happened |
| `type` | string | See the list below |
| `team`, `player` | string | Who did it |
| `x`, `y` | float | Where it started |
| `end_x`, `end_y`, `t_end` | float | Where and when the ball arrived (passes, carries, shots, crosses, restarts) |
| `receiver` | string | Intended receiver of a pass |
| `outcome` | string | `complete`, `intercepted`, `out`, `blocked`, `saved`, `off_target`, `goal`, `cleared`, `claimed`… |
| `possession` | string | Team in possession **after** this event |
| `incident` | string | Set on the event that *is* an incident (`INC-01`…) |
| `scripted_block` | string | Set on every event that came from the match script |
| `on_field_call` | string | For incident moments: what the referee called live (`none`, `foul`) |

Event types:

- **On the ball:** `kick_off`, `pass`, `carry`, `cross`, `shot`, `header_pass`, `clearance`, `throw_in`
  (`long_throw: true` for long throws), `goal_kick`, `corner`, `free_kick` (`kind: direct | indirect`),
  `penalty_kick`
- **Defending:** `pressure` (`on_player`), `tackle`, `interception`, `ball_recovery`, `save`
  (`gk_hold_s`), `claim` (`gk_hold_s`)
- **Officiating:** `foul`, `card` (`card: yellow | red`, `replaces`), `var_check` (`check`, `outcome`,
  `duration_s`, `concurrent_with_play`), `on_field_review` (`outcome`, `duration_s`), `decision`,
  `announcement` (`text`: what the referee says to the stadium)
- **Incident moments the referee did not call:** `challenge_contact`, `ball_arm_contact`, `grappling`
  (`until_t`, `mutual`)
- **Match flow:** `goal` (`status: confirmed | provisional`; a provisional goal can be cancelled by a
  later `decision`), `ball_out` (`out_over`), `substitution` (`replaces`), `injury_stoppage`,
  `added_time_board` (`minutes`), `period_start`, `period_end`

## `stoppages.json`

```json
{"by_period": {"1": {"ball_in_play_s": 1672.86, "added_time_announced_min": 5, "by_type": {...}}},
 "ledger": [{"stoppage_id": "S0001", "period": 1, "type": "free_kick", "start_t": 42.28, "end_t": 58.36,
             "duration_s": 16.08, "chargeable": false, "linked_event": "E00021"}]}
```

- `chargeable: true` means officials add this time back: goals, substitutions, injuries, cards,
  VAR checks, on-field reviews, penalties, a sent-off player leaving.
- The board at 45:00 shows **75% of the chargeable time**, rounded. That is this model's
  calibration, not a rule. Time lost after the board goes up is added on top.
- This match: about 55 minutes of ball in play, +5 in the first half and +8 in the second.

## `tracking_1hz.csv`

Columns: `period, t, entity, x, y, ball_in_play`. Only players on the pitch at that second appear.
Players hold a 4-3-3 shape that shifts with the ball, the nearest two defenders press, the ball
carrier follows the ball, and every event pins its player to the event location.

## `incidents/INC-0X.json`

```json
{
  "incident_id": "INC-01", "period": 1, "t": 1390.0, "clock": "23:10",
  "type": "penalty_claim",
  "headline": "Penalty shout: Blake goes down under Lund's sliding challenge",
  "attacking_team": "KES", "defending_team": "NBR",
  "involved": {"attacker": {"id": "KES-09", "name": "Conor Blake"}, "defender": {...}},
  "focal": {"dt": 0.0, "x": 12.0, "y": 30.8, "what": "point of contact on the attacker's ankle"},
  "critical_window": [-0.5, 0.5],
  "observers": {"REF": "Daniel Whitlock"},
  "officiating": {"on_field_decision": "play_on", "var": {...}, "final_decision": "no_penalty",
                  "stadium_announcement": null, "restart": "corner to Kestrel"},
  "laws": ["Law 12: fouls and misconduct (careless challenge)"],
  "linked_events": ["E00407", "E00408", "..."],
  "window": {"hz": 10, "t0": 1390.0, "dt": [-8.0, -7.9, ...],
             "positions": {"KES-09": [[x, y], ...], "REF": [[x, y], ...], "BALL": [[x, y], ...]}}
}
```

- `headline` is written neutrally, as a live caption would be: it never gives away the outcome.
- `officiating` holds the outcome. The MCP server withholds it until `get_officiating_status` says
  the officials have settled the incident; agents never read this file directly.
- `dt` is seconds relative to the key moment (`dt = 0`). `t0 + dt` gives the half's clock.
- `focal` is the point an official needed to see. `critical_window` is the `dt` range that matters.
- `observers` lists which officials' views the Referee's Eye checks (`AR1` for the offside).
- `facing_target` (handball only): the point the defender is facing, for the body-angle check.

## `analysis/INC-0X.json` (Referee's Eye tool output)

| Field | Meaning |
|---|---|
| `target` | What the official needed to see: its height (ankle 0.15 m, arm 1.55 m…) and width (0.5 m) |
| `observers.<id>.visible_share_at_key_moment` | Share of the target the official could see at `dt = 0` (0 = fully hidden, 1 = clear) |
| `observers.<id>.mean_visible_share_in_critical_window` | Same, averaged over the critical window |
| `observers.<id>.blockers`, `blocker_mean_cover` | Who hid it, and how much of the target each one covered on average |
| `observers.<id>.longest_mostly_hidden_run_s` | Longest spell with less than half of the target visible |
| `observers.<id>.distance_to_focal_m` | How far the official was from the key point |
| `observers.<id>.view.side_on_score_deg` | 90 = side-on (best view); under 30 = looking along the run, so depth is hard to judge |
| `observers.<id>.frames` | Every 10 Hz frame where visibility was below 99%, with who covered what |
| `best_view` | The spot the referee could have reached (7 m/s from where he was 1.5 s earlier) with the best view: `best.position`, `best.visible_share`, `move_m.left` / `move_m.closer`, and a one-line `coaching_note` |
| `behind_body` | Handball: angle between where the defender faced and where the referee stood; over 110° means the referee saw his back |
| `offside` | Second-last defender, offside line, `margin_m`, attacker speed and how far he moves per frame at 10 and 50 fps, the assistant's alignment and the angle the margin covers from where she stood |
| `dogso_cover` | For each covering defender, the best time margin to reach the attacker's path (negative means he could not get there), plus the keeper |
| `attack` | `numbers` (attackers v outfield defenders goal side), `chance_value_from_here`, `chance_value_if_unchallenged` (when only the keeper is left), `projected_ball_destination` and `chance_value_at_destination` (blocked crosses) |
| `referee_coverage` | Referee's median distance to the ball this half, distance at the incident, and how far he was from his usual spot for play in that area |
| `incident_occupancy` | 5 m grid of player-seconds per team in the run-up, for the incident-window heatmap |

Visibility is 2.5D: in plan view each body is a 0.35 m disc and we measure the share of the
target's angular width they hide; in elevation a body only hides the target if the line from the
official's eye (1.70 m) to the target passes below the top of that player's head.

`chance_value` is a deliberately simple chance-quality model (distance and angle to goal). It is
labelled as such in the UI. A central penalty-spot shot comes out around 0.23.

## `heatmaps.json`

- `players.<id>`: 21 × 14 grid of 5 m cells, `cells[ix][iy]`, in seconds with the ball in play.
  **Team attacking frame:** x = 105 is always the opponent's goal, so both halves add up.
- `referee."1"` / `referee."2"`: same grid in the **pitch frame**, one per half.

## The five incidents in `match_001`

| Id | Clock | What happens | Live call | VAR | Final | What the Referee's Eye shows |
|---|---|---|---|---|---|---|
| INC-01 | 23:10 | Blake (KES 9) goes down under Lund's (NBR 4) slide; ball played first | Play on | Check, 52 s | No penalty | 0% of the contact visible: Okafor (NBR 5) in the line of sight; 8 m to his right he would have seen it |
| INC-02 | 38:40 | Osei (KES 11) scores | Goal | Semi-automated offside, 68 s | Disallowed | Offside by 9 cm; the assistant stood 0.35 m off the line, the margin covered 0.2° from where she was |
| INC-03 | 57:20 | Cross hits Corrigan's (NBR 3) raised arm in the box | Play on | On-field review | Penalty, scored 60:35 | Referee 115° behind the defender, so he saw his back; the cross was heading to a spot worth about 0.25 |
| INC-04 | 71:05 | Kettle (KES 5) trips Duarte (NBR 11), through on goal | Yellow | On-field review | Red card | No covering defender could reach the path (best margin −0.78 s); only the keeper left; ref directly behind the play |
| INC-05 | 88:15 | Brandt (NBR 9) and Marchetti (KES 6) grapple at a corner | Play on | Check during play, 40 s | No penalty | 0% visible: Whitfield (KES 8) and Okafor (NBR 5) in the line; holding was mutual |

Also scripted: Brandt's header at 13:20 (1–0) and Demir's 90+04:30 header from a long throw and
near-post flick-on (1–2), scored by ten men.

## Changing the match

Everything scripted lives in `generator/match_script.json`:

- `teams`, `officials`, `substitutions`: names, shirt numbers, 4-3-3 slots, sub timings.
- `scripted[]`: each block has a `period`, an anchor time `t`, a list of `events` with `dt`
  (seconds from the anchor), and an `after` list of stoppages, events and the restart.
- `incident.keyframes`: `{"entity": [[dt, x, y], ...]}`. Key actors follow these exactly
  (smooth spline). Everyone else comes from the 1 Hz simulation.
- After any change: `python generate.py && python validate.py`.

## Known limits (say these honestly in the README and video)

- The 1 Hz movement is a simple shape model, not a physics or AI simulation.
- Bodies are discs (0.35 m) with a height. No limbs, no posture (a sliding player still counts as standing), no body orientation except the handball facing point.
- Offside is measured on the body centre. Real semi-automated offside uses the nearest playable body part.
- Chance values come from a two-variable model, not a trained xG model.
