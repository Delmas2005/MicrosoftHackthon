# Architecture

One engine, three screens. Agents reason; tools measure; a verifier decides what reaches a screen.

```
 match feed ──► Spotter ──► template card ──► Laws Clerk ──► Contradicteur ──► Narrator ──► Web PubSub ──► TV panel
 (replay)       (when)      (revision 1)      (which Law)    (is it true?)     (for whom)   (push)          stadium line
                                             │   ▲          │                                         phone "Why?"
                                             ▼   │          ▼
                                       MCP tool server   verify_card (deterministic)
                                       Referee's Eye     no decision before officials
                                       Laws of the Game  every claim has evidence
                                       (Azure AI Search) every number matches it
```

## Stages, mapped to the brief

| Brief stage | Component | Status |
|---|---|---|
| Ingest | `stream/replay.py`: replays the match at any speed to stdout or Azure Event Hubs. VAR outcomes are hidden until each check ends. | Built |
| Interpret | `stream/spotter.py`: raises `incident_detected`, `var_check_started`, `decision_settled` from the feed alone. | Built |
| Explain | `tools/referee_eye.py` (geometry), `agents/card.py` (verifier), Laws Clerk and Contradicteur on Foundry (`agents/pipeline.py`). | Built |
| Render | Cards pushed to three screens through Azure Web PubSub (`stream/pubsub.py`); screens in `web/`, on an Azure Storage static website. | Built |
| Personalize | Narrator agent: stadium line, the fan's card (club, language), audio. | Built |

## Agents (models on Microsoft Foundry, called through the Azure OpenAI endpoint)

| Agent | Job | Tools it calls | Why it needs a model |
|---|---|---|---|
| Analyst (not an AI agent yet) | Picks the measurements for the incident type and drafts claims tied to evidence: today the template narrator does this without a model | `referee_eye`, `best_view`, `attack_value` | Left for later: the template already gets the measurements right |
| Laws Clerk | Finds and cites the clause of the Laws of the Game that applies | Laws index (Azure AI Search over the IFAB PDF) | Reading and applying legal text |
| Contradicteur | Attacks every claim: does the evidence say this? Drops what fails, forces "referee's call" when in doubt | `verify_card`, `referee_eye` | Judging whether a sentence says what the numbers say |
| Narrator | Rewrites the verified claims for each screen, audience, club and language | none (works only from verified claims) | Tone, length, language, empathy |

The **template narrator** (`agents/fallback_narrator.py`) builds a correct card from the tools with no
model at all. If a model call fails, times out, or the Contradicteur rejects every claim, the
template card is shown instead. That is the failure-recovery path judges can see.

## The three rules, in code

`agents/card.py → verify_card` runs on every card before it is shown. Tests: `tests/test_verifier.py`.

1. **Never ahead of the officials.** A `decided` card needs an official decision event on this
   incident, issued after the officials settled it, with the same decision. A `checking` card
   cannot carry a decision or a verdict claim. Breaking this blocks the whole card.
2. **Every claim cites evidence.** `measure` (a path into the Referee's Eye output, recomputed from
   positions at verification time), `event` (in the feed, part of this incident), `law` (one that
   applies), `player`. A claim without evidence is dropped.
3. **Every number in the text matches its evidence.** "45% visible" with evidence 0.0 is dropped.

## Referee's Eye (tools/referee_eye.py)

- **Visibility, 2.5D.** Plan view: bodies are 0.35 m discs; the target (contact, arm, grip) is a
  0.5 m segment square to the sight line; we measure the share of its angular width that bodies
  hide. Elevation: a body only hides the target if the line from the official's eye (1.70 m) to the
  target (ankle 0.15 m, arm 1.55 m…) passes below that player's head. Output: 0 to 1 per frame.
- **Best view.** Every 1 m grid point the referee could reach (7 m/s from his position 1.5 s
  earlier), scored by visibility × side-on angle × distance (10 to 20 m ideal), with a body-angle
  penalty for contacts on the far side of a player. Output: a coaching line, never a judgement.
- **Offside, DOGSO cover, body angle, attack value.** See `docs/SCHEMA.md`.

## RefBench (refbench/)

500 synthetic incidents across five families (penalty contact, offside, handball, DOGSO, holding).
Positions, referee placement and bodies in the line of sight are random; the answer comes from
explicit rules (`refbench/rules.py`) and from measured geometry. The live call is wrong more often
when the referee could see less.

```bash
python -m refbench.build --n 500 --seed 7
python -m refbench.baselines        # on-field baseline and the consistency oracle
python -m refbench.evaluate refbench/predictions/<system>.jsonl
```

On all 500 incidents: trusting the on-field call gets 73.8% of decisions right; the rules oracle
(a consistency check, not a competitor) gets 100%. The agents, on 100 incidents (20 per kind):

| System (same 100 incidents) | Decision accuracy | VAR recall | VAR precision | Overreach | Final accuracy |
|---|---|---|---|---|---|
| Trust the on-field call | 0.82 | 0.00 | n/a | 0.00 | 0.89 |
| Fourth Official agents (gpt-5-mini) | 0.82 | 1.00 | 0.38 | 0.57 | 0.82 |

What RefBench tests: applying the Laws to stated facts, knowing when VAR should and should not step
in, and measuring what the referee could see. What it does not test: perception from video.

## Azure deployment

| Piece | Service | Status |
|---|---|---|
| Agents and models | Microsoft Foundry: gpt-5-mini, gpt-4.1-mini, through the Azure OpenAI endpoint | Live |
| Laws of the Game | Azure AI Search index over the IFAB PDF | Live |
| Push to screens | Azure Web PubSub (`stream/pubsub.py`) | Live |
| Front ends | Azure Storage static website, France Central (`web/deploy.py`): Static Web Apps regions were not open to our subscription | Live |
| Live feed | Azure Event Hubs (`stream/replay.py --sink eventhub`) | Supported in code, not deployed (no free tier) |
| Tools | `mcp_server/server.py` (stdio or `--http`) | Runs locally |
| Evaluations | RefBench scored locally (`refbench/evaluate.py`), tuning and held-out sets; groundedness of every card with the Azure AI Evaluation SDK (`evals/groundedness.py`) | Done |
| Voice | Azure AI Speech (F0), prebuilt neural voices: audio cards made once as MP3 (`web/build_audio.py`), the browser's voice if a recording is missing | Live |
| Traces | OpenTelemetry to Application Insights, shown in the Foundry project's Tracing (`agents/telemetry.py`) | Live |
| Latency | Measured against the real models (`stream/latency.py`, `docs/latency.json`) | Done |
| Tests | GitHub Actions on every push (`.github/workflows/tests.yml`) | Live |

Everything runs on an Azure for Students subscription, on free tiers wherever one exists.
