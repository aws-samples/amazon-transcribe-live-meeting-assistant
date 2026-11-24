# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Drive the deployed Stream Audio/Video page in Chromium, with screen video on or off.

``run_stream_page_audio`` streams audio only: the page shares a stand-in
meeting tab that plays one channel of a recorded call, Chromium's fake
microphone plays the other, and screen video stays off
(test_stream_page_audio.py). The rest of this docstring is about
``run_stream_page``, the screen video run.

The page is opened in Chromium (Playwright) with a fake microphone and a fake
screen picker: a second tab plays a canvas slideshow, and Chromium's
``--auto-select-tab-capture-source-by-title`` makes the page's screen share
pick that tab, so the page's own MediaRecorder records it. The probe signs in
as the Cognito test user, switches on **Also record screen video**, streams for
the given time, stops, and returns the meeting's ID (from the page's **Open
recorded meeting** link) and what the page showed. The tests read the stored
meeting afterwards (test_stream_page_video.py).

The slideshow changes slide every ``SLIDE_SECONDS``, a multiple of the two
seconds at which the transcriber samples key frames (the page asks its encoder
for one every 1.8 seconds), so each slide is held for several samples. Each
slide is shown for longer than ``KEYFRAME_BUSY_GAP_MS`` (videokeyframes.ts):
slides that change sooner make the transcriber's key frame detector treat the
screen as busy, and a busy picture is kept only once it has held still for
``KEYFRAME_DESKTOP_BUSY_SETTLE_MS``, which a slide shorter than the busy gap,
sampled every two seconds, does not reliably do.

Chromium runs headed, because the picker flag picks a tab only there (headless,
the fake picker always gives a synthetic screen); on a machine without a
display, run under ``xvfb-run``. Depends on ``playwright``
(``requirements-video.txt``) and a Chromium it has installed (``python -m
playwright install chromium``).
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field

# The page's route in the web UI (STREAM_AUDIO_PATH in the UI's routes/constants.js).
STREAM_PAGE_PATH = "/stream"
SLIDE_SECONDS = 12
# Backgrounds whose grey levels are far apart, so each slide differs from every
# other by more than the key frame detector's per-cell threshold
# (KEYFRAME_PIXEL_DELTA in videokeyframes.ts).
SLIDE_COLOURS = ("#ffffff", "#202a6a", "#f0d040", "#c03030")
SLIDESHOW_TITLE = "LMA slideshow probe"
SLIDESHOW_HTML = f"""<!doctype html><title>{SLIDESHOW_TITLE}</title>
<body style="margin:0"><canvas id="c" width="1280" height="720"></canvas><script>
const g = document.getElementById('c').getContext('2d');
const colours = {list(SLIDE_COLOURS)!r};
// The clock starts when the probe calls startSlides(), once the page's capture
// has begun: until then slide 1 is shown, so the first slide in the stream is
// never cut short, only the last one may be.
let t0 = null;
window.startSlides = () => {{ if (t0 === null) t0 = performance.now(); }};
let n = 0;
setInterval(() => {{
  n += 1;
  // By the clock rather than by ticks, which a background tab may delay.
  const slide = t0 === null ? 0 : Math.floor((performance.now() - t0) / ({SLIDE_SECONDS} * 1000));
  g.fillStyle = colours[slide % colours.length]; g.fillRect(0, 0, 1280, 720);
  g.fillStyle = slide % 2 ? '#ffffff' : '#101010'; g.font = 'bold 96px sans-serif';
  g.fillText('Slide ' + (slide + 1), 80, 200);
  g.fillRect((n * 7) % 1200, 640, 20, 20);
}}, 200);
</script></body>"""


@dataclass
class PageRun:
    call_id: str | None = None
    video_on_shown: bool = False
    surface_text: str = ""
    # The consent notice as the page showed it with video on, when the
    # deployment shows a consent step, and whether the video switch was then
    # fixed by the confirmation.
    notice_text: str = ""
    video_switch_locked: bool | None = None
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    console: list = field(default_factory=list)


def _call_id_from(href: str) -> str | None:
    match = re.search(r"#/calls/([^?#]+)", href or "")
    return urllib.parse.unquote(match.group(1)) if match else None


def _open_stream_page(page, url: str, username: str, password: str) -> None:
    """Sign in to the web UI and open the Stream Audio/Video page."""
    page.goto(url)
    page.get_by_label(re.compile("^(Username|Email)$")).first.fill(username)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state("networkidle")
    page.goto(url.rstrip("/") + "/#" + STREAM_PAGE_PATH)


def run_stream_page(url: str, username: str, password: str, *, seconds: float, topic: str,
                    headless: bool = False) -> PageRun:
    """Stream ``seconds`` of the slideshow tab from the Stream Audio/Video page, with video on."""
    from playwright.sync_api import sync_playwright

    run = PageRun()
    args = [
        "--use-fake-device-for-media-stream",
        f"--auto-select-tab-capture-source-by-title={SLIDESHOW_TITLE}",
        "--enable-usermedia-screen-capturing",
    ]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=args)
        context = browser.new_context(viewport={"width": 1400, "height": 1000}, permissions=["microphone"])
        slides = context.new_page()
        slides.set_content(SLIDESHOW_HTML)
        page = context.new_page()
        page.on("console", lambda m: run.console.append(m.text[:300]))
        page.on("dialog", lambda d: d.accept())
        _open_stream_page(page, url, username, password)
        switch = page.get_by_role("checkbox", name=re.compile("Also record screen video"))
        switch.wait_for(timeout=30000)
        switch.check()
        page.get_by_label("Meeting Topic").fill(topic)
        notice = page.get_by_test_id("consent-notice")
        if notice.count():
            # A Notify deployment: confirm the notice was given out loud, so the
            # transcriber records it on the meeting's consent record.
            run.notice_text = notice.first.inner_text()
            page.get_by_role("checkbox", name=re.compile("I have told everyone in the meeting")).check()
            page.get_by_role("radio", name=re.compile("Out loud")).check()
            run.video_switch_locked = switch.is_disabled()
        page.get_by_role("button", name="Start Streaming").click()
        try:
            page.get_by_text("Screen video is ON").wait_for(timeout=45000)
            run.video_on_shown = True
            # Capture has begun: the slideshow's clock starts now (see SLIDESHOW_HTML).
            slides.evaluate("window.startSlides()")
            run.surface_text = page.get_by_text(re.compile("^Recording .* as video")).first.inner_text()
        except Exception as err:  # noqa: BLE001 — reported by the test, with the page's state
            run.errors.append(f"no 'Screen video is ON': {err}")
        # If the video never came on, start the slides anyway so the run is the same length.
        slides.evaluate("window.startSlides()")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
        for header in ("Your whole screen is shared", "Some slides may be missed", "Screen video stopped"):
            if page.get_by_text(header).count():
                run.warnings.append(header)
        page.get_by_role("button", name="Stop Streaming").click()
        link = page.get_by_role("link", name=re.compile("Open recorded meeting"))
        link.wait_for(timeout=30000)
        run.call_id = _call_id_from(link.get_attribute("href") or "")
        page.wait_for_timeout(3000)
        browser.close()
    return run


# ── audio only ─────────────────────────────────────────────────────────────

#: The title of the stand-in meeting tab the page shares in an audio-only run.
MEETING_TAB_TITLE = "LMA stream page audio probe"


def meeting_tab_html(audio_file: str) -> str:
    """A stand-in meeting tab that plays one channel of a recorded call, looped."""
    return (f"<!doctype html><title>{MEETING_TAB_TITLE}</title>"
            f"<body><h1>{MEETING_TAB_TITLE}</h1>"
            f'<audio src="{audio_file}" autoplay loop controls></audio></body>')


@dataclass
class AudioPageRun:
    call_id: str | None = None
    streaming_shown: bool = False
    #: The notice the consent step showed, or "" when it showed none.
    notice_shown: str = ""
    #: The START frame the page sent, and how many binary (audio) frames followed.
    start_frame: dict = field(default_factory=dict)
    audio_frames: int = 0
    start_video_sent: bool = False
    errors: list = field(default_factory=list)
    console: list = field(default_factory=list)


def _read_sent_frames(run: AudioPageRun, frames: list) -> None:
    for payload in frames:
        if not isinstance(payload, str):
            run.audio_frames += 1
            continue
        try:
            message = json.loads(payload)
        except ValueError:
            continue
        if message.get("callEvent") == "START" and not run.start_frame:
            run.start_frame = message
        elif message.get("callEvent") == "START_VIDEO":
            run.start_video_sent = True


def run_stream_page_audio(url: str, username: str, password: str, *, seconds: float, topic: str,
                          meeting_url: str, microphone_wav: str, confirm_notice: bool = False,
                          headless: bool = False) -> AudioPageRun:
    """Stream ``seconds`` of audio from the Stream Audio/Video page, with screen video off.

    The page shares the stand-in meeting tab at ``meeting_url``, which
    ``--auto-select-tab-capture-source-by-title`` picks, so the tab's audio is
    the meeting's; Chromium's fake microphone plays ``microphone_wav``. With
    ``confirm_notice``, the consent step's notice is read and confirmed as
    given in the meeting chat before Start.
    """
    from playwright.sync_api import sync_playwright

    run = AudioPageRun()
    args = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={microphone_wav}",
        f"--auto-select-tab-capture-source-by-title={MEETING_TAB_TITLE}",
        "--enable-usermedia-screen-capturing",
        "--autoplay-policy=no-user-gesture-required",
    ]
    frames: list = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=args)
        context = browser.new_context(viewport={"width": 1400, "height": 1000}, permissions=["microphone"])
        meeting = context.new_page()
        meeting.goto(meeting_url)
        page = context.new_page()
        page.on("console", lambda m: run.console.append(m.text[:300]))
        page.on("dialog", lambda d: d.accept())
        page.on("websocket", lambda ws: ws.on("framesent", lambda payload: frames.append(payload)))
        _open_stream_page(page, url, username, password)
        topic_field = page.get_by_label("Meeting Topic")
        topic_field.wait_for(timeout=30000)
        video = page.get_by_role("checkbox", name=re.compile("Also record screen video"))
        if video.count() and video.is_checked():
            video.uncheck()
        topic_field.fill(topic)
        if confirm_notice:
            notice = page.get_by_test_id("consent-notice")
            try:
                notice.wait_for(timeout=30000)
                run.notice_shown = notice.inner_text().strip()
                page.get_by_role("checkbox", name=re.compile("I have told everyone")).check()
                page.get_by_role("radio", name=re.compile("In the meeting chat")).check()
            except Exception as err:  # noqa: BLE001 — reported by the test, with the page's state
                run.errors.append(f"no consent notice: {err}")
        page.get_by_role("button", name="Start Streaming").click()
        stop = page.get_by_role("button", name="Stop Streaming")
        try:
            stop.wait_for(timeout=45000)
            run.streaming_shown = True
        except Exception as err:  # noqa: BLE001 — reported by the test, with the page's state
            run.errors.append(f"no 'Stop Streaming': {err}")
        if run.streaming_shown:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                page.wait_for_timeout(1000)
            stop.click()
            link = page.get_by_role("link", name=re.compile("Open recorded meeting"))
            try:
                link.wait_for(timeout=30000)
                run.call_id = _call_id_from(link.get_attribute("href") or "")
            except Exception as err:  # noqa: BLE001 — reported by the test, with the page's state
                run.errors.append(f"no 'Open recorded meeting' link: {err}")
            page.wait_for_timeout(3000)
        browser.close()
    _read_sent_frames(run, frames)
    run.call_id = run.call_id or run.start_frame.get("callId")
    return run
