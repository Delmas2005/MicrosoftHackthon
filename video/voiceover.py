"""The video's voice-over, read by Azure AI Speech (a prebuilt British neural voice).

    py video/voiceover.py          # one WAV per line in video/voice/, about 1,500 characters (free tier)

Then `py video/record.py` records the screens and lays these lines over the picture, each at its
moment. Every line is checked against its slot: if it would run into the next line or past the end
of its scene, the script says so (shorten the line, or give it a faster `rate`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VOICE = ROOT / "video" / "voice"
INDEX = VOICE / "index.json"

# (scene, seconds into the scene, text, speaking rate). Scene lengths are in video/record.py.
LINES = [
    ("title", 0.6, "Seventy-four per cent of fans say VAR decisions are not clear. "
                   "Cameras show what VAR saw. Never what the referee could see.", "+8%"),
    ("tv", 0.5, "During the VAR check, the Fourth Official replays the incident from tracking data. "
                "The yellow cone is what the referee could see.", "+3%"),
    ("tv", 10.2, "At the moment of contact: nothing. Number five was in his line of sight.", "0%"),
    ("decision", 0.4, "Nothing appears before the officials decide. Every sentence is tied to a measurement, or to the Laws of the Game.", "+5%"),
    ("agents", 0.5, "Three agents on Microsoft Foundry write the cards. The Laws Clerk quotes the Laws word for word, "
                    "from Azure AI Search.", "0%"),
    ("agents", 10.0, "The Contradicteur removes any sentence that says more than its evidence. Here, it catches a planted mistake.", "0%"),
    ("wall", 0.5, "One decision, every screen: the broadcast, the stadium board, and the supporter's phone, in their language.", "0%"),
    ("wall", 8.3, "And an audio version, read by Azure AI Speech, for fans who cannot see the screen.", "0%"),
    ("bestview", 0.4, "Coaching, not blame: ten metres to his left, he would have seen the holding.", "0%"),
    ("refbench", 0.5, "On incidents they had never seen, the agents beat the call on the field, but call VAR too often. "
                      "So they explain decisions. They never make them.", "+5%"),
    ("architecture", 0.5, "All of it runs on an Azure for Students account: free tiers, student credit, and no credit card.", "0%"),
    ("end", 0.3, "The Fourth Official. Built by two students, without a budget.", "0%"),
]


def main():
    from tools.speech import synthesize, wav_seconds
    from video.record import SCENES  # noqa: the scene lengths

    length = {name: seconds - 0.2 for name, _, seconds, _ in SCENES}
    VOICE.mkdir(exist_ok=True)
    index, chars, problems = [], 0, 0
    for i, (scene, at, text, rate) in enumerate(LINES):
        wav = synthesize(text, "en", fmt="wav", rate=rate)
        name = f"{i:02d}-{scene}.wav"
        (VOICE / name).write_bytes(wav)
        dur = wav_seconds(wav)
        chars += len(text)
        nxt = next((a for s, a, *_ in LINES[i + 1:] if s == scene), length[scene])
        ok = at + dur <= nxt
        problems += not ok
        print(f"  {name:<20} {dur:4.1f} s  in a slot of {nxt - at:4.1f} s  {'ok' if ok else 'TOO LONG'}")
        index.append({"file": name, "scene": scene, "at": at, "seconds": round(dur, 2)})
    INDEX.write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"{len(index)} lines, {chars} characters. {'All fit.' if not problems else f'{problems} too long: shorten them.'}")


if __name__ == "__main__":
    main()
