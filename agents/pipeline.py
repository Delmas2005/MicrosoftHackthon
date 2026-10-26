"""The agent desk: puts the AI agents into the live pipeline without ever slowing it down.

For every card the live pipeline needs:
1. the template card (agents/fallback_narrator.py) is verified and published at once (revision 1);
2. in the background, the agents improve it (revision 2):
   - the Laws Clerk cites the clause of the Laws that applies (once per incident, reused);
   - the Contradicteur reviews every claim and removes the ones that go too far.
   When that is done, revision 2 replaces revision 1 on the screens.
3. once a decision is settled, the Narrator writes the stadium line, the fan's "Why?" card (their
   club, their language) and the audio version from the reviewed TV card (revision 3), each one
   verified and reviewed again.

If an agent is slow or fails, nothing is lost: revision 1 is already on screen, and the agents
fall back to the template sentence themselves. Screens show, for each (incident, phase, audience),
the card with the highest revision.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor

from agents.card import MatchData
from agents.contradicteur import review_card
from agents.fallback_narrator import LAW_LINE, checking_card, decided_card
from agents.laws_clerk import law_claim
from agents.narrator import narrate_all

# Keep this low: every request reserves its token budget against the deployment's tokens-per-minute.
MAX_PARALLEL_CALLS = 2


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
            self.law_claims[inc_id] = self.pool.submit(law_claim, inc, LAW_LINE[inc["type"]][1])

    def _law_claim(self, inc_id: str) -> dict:
        return self.law_claims[inc_id].result()

    # The Law lookup is always queued before the jobs that wait for it, so the pool (first in,
    # first out) always starts it first: a job can never wait for work stuck behind itself.

    def checking(self, inc_id: str, at: dict, check_event: str, audience: str = "tv") -> None:
        self.start_incident(inc_id)

        def job():
            card = checking_card(self.data, inc_id, at, check_event, audience)
            claim = self._law_claim(inc_id)
            if claim["author"] == "laws_clerk":  # only the agent's citation: the template line is for decided cards
                card["claims"].append(claim)
            return self._finish(card)
        self.pending.append(self.pool.submit(job))

    def decided(self, inc_id: str, at: dict, audiences=("tv", "stadium")) -> None:
        """One job for all the screens of an incident: "referee's call" is a judgement about the
        incident, so every screen must show the same one. It is decided on the first audience
        (the TV card, which has the most evidence) and applied to the others."""
        self.start_incident(inc_id)

        def job():
            claim = self._law_claim(inc_id)
            results = []
            for audience in audiences:
                card = decided_card(self.data, inc_id, at, audience)
                card["claims"] = [claim if c["id"] == "law" else c for c in card["claims"]]
                results.append(self._finish(card))
            first = results[0]["review"]
            for r in results[1:]:
                r["review"]["referees_call"] = first["referees_call"]
                r["review"]["referees_call_reason"] = first.get("referees_call_reason", "")
                r["card"].pop("framing", None)
                if first["referees_call"]:
                    r["card"]["framing"] = "referees_call"
            # The Narrator works from the reviewed TV card (or the template one if nothing survived).
            tv = results[0]["card"] if results[0]["published"] else decided_card(self.data, inc_id, at, "tv")
            for n in narrate_all(tv, self.data, self.fan_language):
                if n["published"]:
                    results.append(n)
                else:
                    results.append({"card": None, "published": False,
                                    "error": f"Narrator, {n['audience']} card not shown: {n['problems']}"})
            return results
        self.pending.append(self.pool.submit(job))

    def _finish(self, card: dict) -> dict:
        card["author"] = "agents"
        card["revision"] = 2
        result = review_card(card, self.data)
        result["published"] = bool(result["card"]["claims"])
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
