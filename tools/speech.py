"""Azure AI Speech, text to speech, through its REST API (no extra package).

Needs in .env: AZURE_SPEECH_KEY and AZURE_SPEECH_REGION (for example francecentral), from a Speech
resource on the free tier (F0: half a million characters of neural voices a month, free).
"""
from __future__ import annotations

import os
import urllib.request
from xml.sax.saxutils import escape

from dotenv import load_dotenv

# One prebuilt neural voice per language the fan card can use.
VOICES = {"en": "en-GB-RyanNeural", "es": "es-ES-AlvaroNeural", "fr": "fr-FR-HenriNeural", "pt": "pt-PT-DuarteNeural",
          "de": "de-DE-ConradNeural", "it": "it-IT-DiegoNeural", "nl": "nl-NL-MaartenNeural"}
LOCALES = {"en": "en-GB", "es": "es-ES", "fr": "fr-FR", "pt": "pt-PT", "de": "de-DE", "it": "it-IT", "nl": "nl-NL"}
FORMATS = {"mp3": "audio-24khz-48kbitrate-mono-mp3", "wav": "riff-24khz-16bit-mono-pcm"}


def synthesize(text: str, language: str = "en", fmt: str = "mp3", rate: str = "0%") -> bytes:
    load_dotenv()
    key, region = os.environ["AZURE_SPEECH_KEY"], os.environ["AZURE_SPEECH_REGION"]
    lang = language if language in VOICES else "en"
    ssml = (f'<speak version="1.0" xml:lang="{LOCALES[lang]}"><voice name="{VOICES[lang]}">'
            f'<prosody rate="{rate}">{escape(text)}</prosody></voice></speak>')
    req = urllib.request.Request(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1", data=ssml.encode("utf-8"), method="POST",
        headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": FORMATS[fmt], "User-Agent": "fourth-official"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def wav_seconds(wav: bytes) -> float:
    """Length of a 24 kHz, 16-bit, mono WAV from synthesize(fmt="wav")."""
    return max(0, len(wav) - 44) / (24000 * 2)
