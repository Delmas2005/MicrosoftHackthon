# The Fourth Official

**VAR sees everything. The stadium understands nothing. We show what the referee could actually see.**

74% of Premier League fans say the reasoning behind VAR decisions is unclear (Football Supporters'
Association, March 2026, ~8,000 fans). VAR audio still cannot be broadcast live. Every camera shows
what VAR saw; none shows what the referee could see from where he stood.

The Fourth Official measures it from tracking data, live, and explains each decision with the Law in hand:

- **TV panel**, during the VAR check: the referee's line of sight, who hid the contact and by how
  much, the Law, what the attack was worth, and where he could have seen it from.
- **Stadium screen**, seconds after the announcement: one line: decision, reason, Law.
- **"Why?" on the phone**: the fan of the club that lost the call, an analyst, or audio description.

Built for the Microsoft × Premier League *Inside the Game* hackathon. Every club, player, official
and venue in this repository is fictional. All data is synthetic.

## Three rules, enforced in code

1. We never announce a decision before the officials do.
2. Every claim on screen cites evidence: a measurement, an event, a Law.
3. Every number on screen matches that evidence.

`agents/card.py` checks every card before it is shown. `tests/` proves it.

## What runs today

```bash
pip install -r requirements.txt
python generate.py                      # synthetic match: Northbridge FC 1-2 Kestrel Rovers, 5 incidents
python validate.py                      # 28 checks on the data
python -m stream.run_live --speed 30    # live replay -> spotter -> cards -> verifier -> printed screens
python -m refbench.build && python -m refbench.baselines   # 500-incident benchmark and baselines
python tests/test_verifier.py && python tests/test_pipeline.py
python -m mcp_server.server             # the tools, over MCP
```

Sample output, 23:10, penalty shout:

```
INC-01 CHECKING tv
  - VAR is checking a possible penalty.
  - The referee was 20 m away and could see 0% of the contact on the attacker's ankle.
  - Idris Okafor, number 5, was in his line of sight.
INC-01 DECIDED tv
  - No penalty.
  - Law 12: contact on its own is not a foul. The challenge has to be careless, reckless or use excessive force.
  - Best view within reach: 8 m to his right and 4 m closer.
```

## Repository

| Path | What |
|---|---|
| `generator/` | Synthetic match generator: events, stoppages, 1 Hz tracking, 10 Hz incident windows |
| `tools/referee_eye.py` | 2.5D visibility, best view, offside, DOGSO cover, body angle, attack value |
| `agents/card.py` | Decision card format and the verifier |
| `agents/fallback_narrator.py` | Model-free cards: the safety net when a model fails |
| `stream/` | Live replay (stdout or Azure Event Hubs), incident spotter, end-to-end runner |
| `mcp_server/` | MCP server exposing every tool to the agents |
| `refbench/` | RefBench: 500 incidents, rules, scorer, baselines |
| `docs/` | `ARCHITECTURE.md`, `SCHEMA.md` (data contract) |

## Limits we state up front

- 2.5D, not 3D: bodies are discs with a height; no limbs or posture.
- The 1 Hz movement model is simple; incidents are keyframed at 10 Hz.
- RefBench tests Law application and measurement, not perception from video.
- Chance values come from a two-variable model, not a trained xG model.
