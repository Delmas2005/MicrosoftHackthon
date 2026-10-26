"""The agent desk: puts the AI agents into the live pipeline without ever slowing it down.

For every card the live pipeline needs:
1. the template card (agents/fallback_narrator.py) is verified and published at once (revision 1);
2. in the background, the agents improve it (revision 2), one job per card:
   - the Laws Clerk cites the clause of the Laws that applies (once per incident, reused);
   - the Contradicteur reviews every claim and removes the ones that go too far.
   When that is done, revision 2 replaces revision 1 on the screens.
3. once a decision is settled, the Narrator writes the stadium line, the fan's "Why?" card (their
   club, their language) and the audio version from the reviewed TV card (revision 3), each one
   verified and reviewed again.

If an agent is slow or fails, nothing is lost: revision 1 is already on screen, and the agents
fall back to the template sentence themselves. Screens show, for each (incident, phase, audience),
the card with the highest revision.

Every result carries `latency_s`: seconds from the moment the pipeline asked for it (VAR check
started, or decision settled) to the moment it was ready, queueing included. Every step is also
timed and traced (agents/telemetry.py).
"""
from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor

from agents.card import MatchData
from agents.contradicteur import review_card
from agents.fallback_narrator import LAW_LINE, checking_card, decided_card
from agents.laws_clerk import law_claim
from agents.narrator import AUDIENCES as NARRATOR_AUDIENCES
from agents.narrator import narrate_safe
from agents.telemetry import span

# Every request reserves its token budget against the deployment's tokens-per-minute: 4 at a time
# fits our quotas (gpt-5-mini 200K, gpt-4.1-mini 50K) and lets the 3 Narrator cards run together.
MAX_PARALLEL_CALLS = 4


class AgentDesk:
    def __init__(self, data: MatchData, workers: int = MAX_PARALLEL_CALLS, fan_language: str = "es"):
        self.data = data
        self.fan_language = fan_language
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.law_claims: dict[str, Future] = {}
        self.pending: list[Future] = []

    # ------------------------------------------------------------------ jobs

    def start_incident(self, inc_id: str) -> None:
        """Start looking up the Law as soon as an incident is detected: it is ready sooner."""
        if inc_id not in self.law_claims:
            inc = self.data.incident(inc_id)
            self.law_claims[inc_id] = self.pool.submit(self._cite, inc_id, inc)

    @staticmethod
    def _cite(inc_id: str, inc: dict) -> dict:
        with span(f"laws_clerk {inc_id}", agent="laws_clerk", incident=inc_id):
            return law_claim(inc, LAW_LINE[inc["type"]][1])

    def _law_claim(self, inc_id: str) -> dict:
        return self.law_claims[inc_id].result()

    # The Law lookup is always queued before the jobs that wait for it, so the pool (first in,
    # first out) always starts it first: a job can never wait for work stuck behind itself.

    def checking(self, inc_id: str, at: dict, check_event: str, audience: str = "tv") -> None:
        self.start_incident(inc_id)
        asked = time.perf_counter()

        def job():
            with span(f"checking {inc_id}", incident=inc_id, phase="checking"):
                card = checking_card(self.data, inc_id, at, check_event, audience)
                claim = self._law_claim(inc_id)
                if claim["author"] == "laws_clerk":  # only the agent's citation: the template line is for decided cards
                    card["claims"].append(claim)
                return self._stamp(self._finish(card), asked)
        self.pending.append(self.pool.submit(job))

    def decided(self, inc_id: str, at: dict, audiences=("tv", "stadium")) -> None:
        """One job per card, so each card reaches the screens as soon as it is ready:
        - the TV card and the stadium board are reviewed at the same time;
        - "referee's call" is a judgement about the incident, so every screen must show the same one:
          it is decided on the TV card (the one with the most evidence) and applied to the others;
        - the Narrator's three cards start as soon as the TV card is reviewed, all three together.
        Jobs only ever wait for jobs submitted before them, so the pool (first in, first out)
        cannot deadlock."""
        self.start_incident(inc_id)
        asked = time.perf_counter()
        first, *others = audiences

        def review(audience):
            with span(f"decision {inc_id} {audience}", incident=inc_id, phase="decided", audience=audience):
                claim = self._law_claim(inc_id)
                card = decided_card(self.data, inc_id, at, audience)
                card["claims"] = [claim if c["id"] == "law" else c for c in card["claims"]]
                return self._finish(card)

        tv_job = self.pool.submit(lambda: self._stamp(review(first), asked))
        self.pending.append(tv_job)

        def tv_result():
            try:
                return tv_job.result()
            except Exception:  # the TV job crashed: the other screens keep their own review
                return None

        def other(audience):
            r = review(audience)
            tv = tv_result()
            if tv is not None:
                r["review"]["referees_call"] = tv["review"]["referees_call"]
                r["review"]["referees_call_reason"] = tv["review"].get("referees_call_reason", "")
                r["card"].pop("framing", None)
                if tv["review"]["referees_call"]:
                    r["card"]["framing"] = "referees_call"
            return self._stamp(r, asked)

        def narrate(audience):
            tv = tv_result()
            # The Narrator works from the reviewed TV card (or the template one if nothing survived).
            source = tv["card"] if tv and tv["published"] else decided_card(self.data, inc_id, at, "tv")
            n = narrate_safe(source, self.data, audience, self.fan_language)
            if not n["published"]:
                n = {"card": None, "published": False, "audience": audience, "t_ready": n["t_ready"],
                     "error": f"Narrator, {audience} card not shown: {n['problems']}"}
            n["latency_s"] = round(n["t_ready"] - asked, 2)
            return n

        for audience in others:
            self.pending.append(self.pool.submit(other, audience))
        for audience in NARRATOR_AUDIENCES:
            self.pending.append(self.pool.submit(narrate, audience))

    def _finish(self, card: dict) -> dict:
        card["author"] = "agents"
        card["revision"] = 2
        with span(f"contradicteur {card['incident_id']} {card['audience']}", agent="contradicteur",
                  incident=card["incident_id"], audience=card["audience"], phase=card["phase"]):
            result = review_card(card, self.data)
        result["published"] = bool(result["card"]["claims"])
        return result

    @staticmethod
    def _stamp(result: dict, asked: float) -> dict:
        result["latency_s"] = round(time.perf_counter() - asked, 2)
        return result

    # ------------------------------------------------------------------ results

    def ready(self) -> list[dict]:
        """Results of the jobs that have finished since the last call."""
        done, still_running = [], []
        for f in self.pending:  # one pass: a job finishing right now goes in exactly one list
            (done if f.done() else still_running).append(f)
        self.pending = still_running
        return [r for f in done for r in self._results(f)]

    def drain(self) -> list[dict]:
        """Wait for every job still running (end of the match)."""
        out = [r for f in self.pending for r in self._results(f)]
        self.pending = []
        self.pool.shutdown(wait=True)
        return out

    @staticmethod
    def _results(f: Future) -> list[dict]:
        try:
            r = f.result()
            return r if isinstance(r, list) else [r]
        except Exception as exc:  # a job that crashed: revision 1 stays on screen
            return [{"card": None, "published": False, "error": f"{type(exc).__name__}: {exc}"}]
