"""Push cards to the screens live, through Azure Web PubSub.

The live runner sends every published card to the hub; every screen open with a viewer link
receives it within a second and switches to that incident.

    py -m stream.pubsub --viewer-url https://<your-site>.azurestaticapps.net   # link for the screens
    py -m stream.run_live --agents --publish --speed 5 --from 1:22:30        # then play the match

The viewer link carries a token that can only *receive* (no send rights) and expires after
--hours (default 24). The connection string itself stays in .env and never leaves your computer.
Needs WEBPUBSUB_CONNECTION_STRING in .env and `py -m pip install azure-messaging-webpubsubservice`.
"""
from __future__ import annotations

import argparse
import json
import os
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()
HUB = os.environ.get("WEBPUBSUB_HUB", "fourth_official")


def _client():
    from azure.messaging.webpubsubservice import WebPubSubServiceClient

    return WebPubSubServiceClient.from_connection_string(os.environ["WEBPUBSUB_CONNECTION_STRING"], hub=HUB)


class Publisher:
    """Sends cards to every connected screen. A failed send is reported, never fatal."""

    def __init__(self):
        self.client = _client()
        self.sent = self.failed = 0

    def send(self, card: dict) -> bool:
        try:
            self.client.send_to_all(json.dumps(card, ensure_ascii=False), content_type="application/json")
            self.sent += 1
            return True
        except Exception as exc:  # the console output and the JSONL file still have the card
            self.failed += 1
            print(f"  (Web PubSub: card not sent, {type(exc).__name__}: {exc})", flush=True)
            return False


def viewer_url(hours: int = 24) -> str:
    """A receive-only WebSocket URL for the screens."""
    return _client().get_client_access_token(minutes_to_expire=hours * 60)["url"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--viewer-url", metavar="SITE", nargs="?", const="http://localhost:8000",
                    help="print the screens' live link for this site (default: the local test server)")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()
    ws = viewer_url(args.hours)
    site = (args.viewer_url or "http://localhost:8000").rstrip("/")
    print(f"Live link for the screens (valid {args.hours} h, receive only):\n")
    for screen in ("wall", "tv", "stadium", "phone"):
        print(f"  {screen:<8} {site}/?screen={screen}&live={quote(ws, safe='')}")


if __name__ == "__main__":
    main()
