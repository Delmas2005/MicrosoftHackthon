"""Read the audio cards aloud with Azure AI Speech, once, and put the recordings on the site.

    py web/build_data.py      # the cards
    py web/build_audio.py     # one MP3 per audio card, in web/audio/
    py web/deploy.py

The phone's Audio tab then plays a neural voice from Azure AI Speech instead of the browser's own
voice (which it still uses if a recording is missing). The keys never reach the site: the voices
are made here, on your machine, and only the MP3 files are published. A recording is only made
again when the text of its card changes. Free tier (F0): about 1,500 characters for the 4 cards.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.speech import synthesize  # noqa: E402

WEB = ROOT / "web"
AUDIO = WEB / "audio"
INDEX = AUDIO / "index.json"


def card_text(card: dict) -> str:
    return " ".join(c["text"] for c in card["claims"])


def audio_key(card: dict) -> str:
    """Same text, same language -> same recording."""
    return hashlib.sha1(f"{card.get('language', 'en')}|{card_text(card)}".encode("utf-8")).hexdigest()[:12]


def attach(cards: list[dict]) -> int:
    """Point every audio card that has a recording at it (also called by build_data.py)."""
    index = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    n = 0
    for c in cards:
        f = index.get(audio_key(c)) if c.get("audience") == "audio" else None
        if f and (AUDIO / f).exists():
            c["audio_file"] = f"audio/{f}"
            n += 1
    return n


def main():
    bundle_path = WEB / "data" / "bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    AUDIO.mkdir(exist_ok=True)
    index = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    chars = 0
    for c in bundle["cards"]:
        if c.get("audience") != "audio":
            continue
        k = audio_key(c)
        name = f"{c['incident_id']}-{c.get('language', 'en')}-{k}.mp3"
        if index.get(k) == name and (AUDIO / name).exists():
            print(f"  {c['incident_id']}: already recorded")
            continue
        text = card_text(c)
        (AUDIO / name).write_bytes(synthesize(text, c.get("language", "en")))
        index[k] = name
        chars += len(text)
        print(f"  {c['incident_id']}: recorded {name} ({len(text)} characters)")
    INDEX.write_text(json.dumps(index, indent=1), encoding="utf-8")
    n = attach(bundle["cards"])
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{n} audio cards now play an Azure AI Speech recording ({chars} characters synthesised this time).")


if __name__ == "__main__":
    main()
