"""Record the demo video (under 2 minutes, 2560x1440, with the voice-over) from the real screens.

    py -m pip install playwright imageio-ffmpeg
    py -m playwright install chromium
    py web/build_data.py                 # the screens' data, with the latest agents' cards
    py video/voiceover.py                # optional: the voice-over, with Azure AI Speech
    py video/record.py                   # -> video/fourth_official_demo.mp4

It serves the repo locally, opens each scene in a headless browser, records it, adds the captions,
and joins everything with ffmpeg. If video/voice/ holds the voice-over, it is laid over the picture. Scenes: title, TV panel (INC-01 live replay then the decision),
the agents' real output, the three screens together, the best view (INC-05), RefBench, the Azure
architecture, the closing line.
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "video" / "fourth_official_demo.mp4"
PORT = 8770
BASE = f"http://localhost:{PORT}"

# Subtitles: the voice-over's own words, small, at the bottom (at the top over the wall, where the
# stadium board is at the bottom).
CAPTION_JS = """([text, top]) => {
  let c = document.getElementById('vid-caption');
  if (!c) {
    const f = new FontFace('Barlow', 'url(/web/fonts/barlow-latin-600-normal.woff2)', { weight: '600' });
    f.load().then((x) => document.fonts.add(x)).catch(() => {});
    c = document.createElement('div'); c.id = 'vid-caption';
    c.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);max-width:1400px;z-index:99;border-radius:6px;' +
      'background:rgba(6,20,13,.78);color:#fff;font:600 30px/1.3 Barlow,sans-serif;padding:10px 24px;text-align:center';
    document.body.append(c);
  }
  c.style.top = top ? '26px' : 'auto'; c.style.bottom = top ? 'auto' : '40px';
  c.textContent = text; c.style.display = text ? 'block' : 'none';
}"""
FADE = 0.4   # crossfade between scenes, seconds
# Subtitles only over the live screens: the other scenes already show their words on screen.
SUBTITLED = {"tv", "decision", "wall", "bestview"}


async def tab(page, frame_selector, tab_name):
    await page.frame_locator(frame_selector).get_by_role("tab", name=tab_name).click()


# (name, url, seconds, [(at_seconds, caption or None, action or None)])
SCENES = [
    ("title", "/video/scenes.html?scene=title", 10.5, []),
    ("tv", "/web/?screen=tv&incident=INC-01&phase=checking&clean", 17.0, [
        (0.3, "A penalty shout, replayed as VAR sees it. The yellow cone is what the referee could see.", None),
        (10.0, "At the contact: 0% visible. Number 5 was in his line of sight.", None),
    ]),
    ("decision", "/web/?screen=tv&incident=INC-01&phase=decided&clean", 9.0, [
        (0.3, "The decision appears only once the officials have made it. Then the agents improve the card.", None),
        (5.0, "Every sentence is tied to a measurement or to the Laws of the Game.", None),
    ]),
    ("agents", "/video/scenes.html?scene=agents", 19.0, []),
    ("wall", "/web/?screen=wall&incident=INC-01&phase=decided&clean", 15.0, [
        (0.3, "One decision, every screen: broadcast, stadium board, and the supporter's phone in their language.", None),
        (8.0, "The audio version is read aloud for fans who cannot see the screen.", lambda p: tab(p, ".w-phone", "Audio")),
    ]),
    ("bestview", "/web/?screen=tv&incident=INC-05&phase=decided&clean", 9.0, [
        (0.3, "Coaching, not blame: 10 m to his left, he would have seen the holding.", None),
    ]),
    ("refbench", "/video/scenes.html?scene=refbench", 12.0, []),
    ("architecture", "/video/scenes.html?scene=architecture", 11.0, []),
    ("end", "/video/scenes.html?scene=end", 6.0, []),
]


def voice_lines() -> list[dict]:
    index = ROOT / "video" / "voice" / "index.json"
    if not index.exists():
        return []
    from video.voiceover import LINES
    text = {f"{i:02d}-{scene}.wav": line for i, (scene, _, line, _) in enumerate(LINES)}
    return [{**v, "text": text.get(v["file"], "")} for v in json.loads(index.read_text(encoding="utf-8"))]


def timeline(scene: str, cues: list) -> list:
    """What happens in a scene, in order. With a voice-over, the subtitles are its words, shown
    while each line is spoken; without one, the scene's own captions."""
    lines = voice_lines()
    if not lines:
        return sorted(cues, key=lambda c: c[0])
    events = [(at, None, action) for at, _, action in cues if action is not None]
    for v in lines:
        if v["scene"] == scene and v["text"] and scene in SUBTITLED:
            events += [(v["at"], v["text"], None), (v["at"] + v["seconds"] + 0.25, "", None)]
    return sorted(events, key=lambda e: e[0])


def scene_starts() -> dict:
    """Where each scene starts in the final video (scenes overlap by FADE)."""
    start, t = {}, 0.0
    for name, _, seconds, _ in SCENES:
        start[name] = t
        t += seconds - 0.2 - FADE
    return start


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("localhost", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


FPS = 30
SCALE = 4 / 3   # pages laid out at 1920 x 1080, rendered at 2560 x 1440: sharp text, and YouTube's better 1440p encoding
# CSS animations and transitions follow the page's controlled clock too (see record()).
ANIMATIONS_JS = """(now) => { const m = window.__anim || (window.__anim = new Map());
  for (const a of document.getAnimations()) { if (!m.has(a)) { m.set(a, now); a.pause(); } a.currentTime = Math.max(0, now - m.get(a)); } }"""


async def record(tmp: Path) -> list[Path]:
    """Every frame is a still screenshot taken on a controlled clock: the page's time (Date,
    timers, requestAnimationFrame, CSS animations) moves exactly 1/30 s between two frames,
    however long a screenshot takes. No screen-recording codec in between: every frame is sharp."""
    from playwright.async_api import async_playwright

    ffmpeg, clips = ffmpeg_exe(), []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for name, url, seconds, cues in SCENES:
            ctx = await browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=SCALE)
            page = await ctx.new_page()
            t0 = 1_793_000_000_000
            await page.clock.install(time=t0)
            await page.clock.pause_at(t0 + 1000)
            await page.goto(BASE + url)
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(600)          # fonts; the page's own clock is not moving
            frames_dir = tmp / name
            frames_dir.mkdir()
            events = timeline(name, cues)
            n = round((seconds - 0.2) * FPS)
            for i in range(n):
                t = i / FPS
                while events and events[0][0] <= t:
                    _, caption, action = events.pop(0)
                    if caption is not None:
                        await page.evaluate(CAPTION_JS, [caption, name == "wall"])
                    if action is not None:
                        await action(page)
                for f in page.frames:
                    try:
                        await f.evaluate(ANIMATIONS_JS, t * 1000)
                    except Exception:
                        pass
                await page.screenshot(path=str(frames_dir / f"{i:05d}.jpg"), type="jpeg", quality=93)
                await page.clock.run_for(round((i + 1) * 1000 / FPS) - round(i * 1000 / FPS))   # whole ms
            await ctx.close()
            clip = tmp / f"{name}.mp4"
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames_dir / "%05d.jpg"),
                            "-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p", str(clip)], check=True)
            shutil.rmtree(frames_dir)
            clips.append(clip)
            print(f"  recorded {name} ({seconds:.0f} s, {n} frames)")
        await browser.close()
    return clips


def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def assemble(clips: list[Path], tmp: Path):
    ffmpeg = ffmpeg_exe()
    parts = clips
    # Join the scenes with short crossfades.
    silent = tmp / "silent.mp4"
    cmd, chain, offset, prev = [ffmpeg, "-y", "-loglevel", "error"], [], 0.0, "0:v"
    for part in parts:
        cmd += ["-i", str(part)]
    for k in range(1, len(parts)):
        offset += SCENES[k - 1][2] - 0.2 - FADE
        out = f"v{k}"
        chain.append(f"[{prev}][{k}:v]xfade=transition=fade:duration={FADE}:offset={offset:.3f}[{out}]")
        prev = out
    cmd += ["-filter_complex", ";".join(chain), "-map", f"[{prev}]", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-maxrate", "4M", "-bufsize", "8M", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(silent)]
    subprocess.run(cmd, check=True)
    if not add_voice(silent, OUT):
        shutil.copy(silent, OUT)


def add_voice(video: Path, out: Path) -> bool:
    """Lay the voice-over (video/voiceover.py) over the picture, each line at its moment."""
    index = ROOT / "video" / "voice" / "index.json"
    if not index.exists():
        print("  no voice-over (run py video/voiceover.py for one): silent video")
        return False
    lines = json.loads(index.read_text(encoding="utf-8"))
    start = scene_starts()
    t = sum(s[2] - 0.2 for s in SCENES) - FADE * (len(SCENES) - 1)
    cmd, chains = [ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(video)], []
    for i, line in enumerate(lines, 1):
        cmd += ["-i", str(index.parent / line["file"])]
        ms = int((start[line["scene"]] + line["at"]) * 1000)
        chains.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(1, len(lines) + 1))
    chains.append(f"{mix}amix=inputs={len(lines)}:duration=longest:normalize=0,apad=whole_dur={t:.2f}[voice]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "0:v", "-map", "[voice]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "160k", "-t", f"{t:.2f}", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    print(f"  voice-over: {len(lines)} lines from Azure AI Speech")
    return True


def main():
    if not (ROOT / "web" / "data" / "bundle.json").exists():
        raise SystemExit("Run py web/build_data.py first.")
    httpd = serve()
    tmp = Path(tempfile.mkdtemp(prefix="fo-video-"))
    try:
        clips = asyncio.run(record(tmp))
        assemble(clips, tmp)
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    total = sum(s[2] - 0.2 for s in SCENES) - FADE * (len(SCENES) - 1)
    print(f"{OUT.relative_to(ROOT)}: about {total:.0f} s")


if __name__ == "__main__":
    main()
