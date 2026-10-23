"""Replay a generated match as a live feed.

    python -m stream.replay --speed 20 --from 1:22:30 --until 1:25:00            # print to stdout
    python -m stream.replay --speed 1 --sink eventhub                             # Azure Event Hubs

The stored feed knows how long every VAR check lasted. A live feed does not, so the replay
splits each check into a start event (what is being checked, no outcome) and a *_complete event
when it ends (with the outcome). Downstream code therefore cannot see a decision early.

Event Hubs sink: set EVENTHUB_CONNECTION_STRING and EVENTHUB_NAME, pip install azure-eventhub.
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
import sys
import time
from pathlib import Path

HIDDEN_UNTIL_COMPLETE = ("outcome", "duration_s")


def parse_at(s: str | None):
    """'1:23:10' -> (1, 1390.0); '2:15' -> (1, 15.0)."""
    if not s:
        return None
    parts = s.split(":")
    if len(parts) == 3:
        return (int(parts[0]), int(parts[1]) * 60 + float(parts[2]))
    return (1, int(parts[0]) * 60 + float(parts[1]))


def live_events(events: list[dict]):
    """Events as a live system would receive them, in time order."""
    heap = []
    for i, e in enumerate(events):
        if e["type"] in ("var_check", "on_field_review") and "duration_s" in e:
            start = {k: v for k, v in e.items() if k not in HIDDEN_UNTIL_COMPLETE}
            start["status"] = "started"
            heapq.heappush(heap, (e["period"], e["t"], i, 0, start))
            done = {"event_id": e["event_id"] + "c", "period": e["period"], "t": round(e["t"] + e["duration_s"], 2),
                    "clock": None, "type": e["type"] + "_complete", "incident": e.get("incident"),
                    "check": e.get("check"), "outcome": e.get("outcome"), "started_event": e["event_id"],
                    "duration_s": e["duration_s"]}
            heapq.heappush(heap, (e["period"], done["t"], i, 1, done))
        else:
            heapq.heappush(heap, (e["period"], e["t"], i, 0, e))
    while heap:
        *_, ev = heapq.heappop(heap)
        if ev.get("clock") is None:
            from generator.common import clock_label
            ev["clock"] = clock_label(ev["period"], ev["t"])
        yield ev


def paced(events, speed: float = 1.0, start=None, until=None, realtime=True):
    last = None
    for e in events:
        key = (e["period"], e["t"])
        if start and key < start:
            continue
        if until and key > until:
            return
        if realtime and last and last[0] == key[0] and speed > 0:
            wait = (key[1] - last[1]) / speed
            if wait > 0:
                time.sleep(min(wait, 5.0))
        last = key
        yield e


class StdoutSink:
    def send(self, e):
        sys.stdout.write(json.dumps(e) + "\n")
        sys.stdout.flush()

    def close(self):
        pass


class EventHubSink:
    def __init__(self):
        try:
            from azure.eventhub import EventData, EventHubProducerClient
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("pip install azure-eventhub to use the Event Hubs sink") from exc
        self.EventData = EventData
        self.client = EventHubProducerClient.from_connection_string(
            os.environ["EVENTHUB_CONNECTION_STRING"], eventhub_name=os.environ["EVENTHUB_NAME"])

    def send(self, e):
        batch = self.client.create_batch(partition_key=str(e.get("period")))
        batch.add(self.EventData(json.dumps(e)))
        self.client.send_batch(batch)

    def close(self):
        self.client.close()


def load_events(match_dir: Path) -> list[dict]:
    return [json.loads(l) for l in (match_dir / "events.jsonl").read_text().splitlines()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="data/match_001")
    ap.add_argument("--speed", type=float, default=1.0, help="1 = real time, 20 = twenty times faster")
    ap.add_argument("--from", dest="start", help="period:mm:ss, e.g. 1:22:30")
    ap.add_argument("--until", help="period:mm:ss")
    ap.add_argument("--sink", choices=["stdout", "eventhub"], default="stdout")
    ap.add_argument("--no-wait", action="store_true", help="emit as fast as possible")
    args = ap.parse_args()
    sink = EventHubSink() if args.sink == "eventhub" else StdoutSink()
    try:
        for e in paced(live_events(load_events(Path(args.match))), args.speed, parse_at(args.start), parse_at(args.until), not args.no_wait):
            sink.send(e)
    finally:
        sink.close()


if __name__ == "__main__":
    main()
