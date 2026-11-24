# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Drive the deployed Stream Audio/Video page in Chromium, with screen video on.

The page is opened in Chromium (Playwright) with a fake microphone and a fake
screen picker: a second tab plays a canvas slideshow, and Chromium's
``--auto-select-tab-capture-source-by-title`` makes the page's screen share
pick that tab, so the page's own MediaRecorder records it. The probe signs in
as the Cognito test user, switches on **Also record screen video**, streams for
the given time, stops, and returns the meeting's ID (from the page's **Open
recorded meeting** link) and what the page showed. The tests read the stored
meeting afterwards (test_stream_page_video.py).

The slideshow changes slide every ``SLIDE_SECONDS``, a multiple of the two
seconds between the key frames the page asks for, so each slide change falls
near a key frame.

Chromium runs headed, because the picker flag picks a tab only there (headless,
the fake picker always gives a synthetic screen); on a machine without a
display, run under ``xvfb-run``. Depends on ``playwright``
(``requirements-video.txt``) and a Chromium it has installed (``python -m
playwright install chromium``).
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field

SLIDE_SECONDS = 6
SLIDESHOW_TITLE = "LMA slideshow probe"
SLIDESHOW_HTML = f"""<!doctype html><title>{SLIDESHOW_TITLE}</title>
<body style="margin:0"><canvas id="c" width="1280" height="720"></canvas><script>
const g = document.getElementById('c').getContext('2d');
const colours = ['#ffffff', '#202a6a', '#f0d040', '#1a5a30'];
let n = 0;
setInterval(() => {{
  n += 1;
  const slide = Math.floor(n / ({SLIDE_SECONDS} * 5));
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
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    console: list = field(default_factory=list)


def _call_id_from(href: str) -> str | None:
    match = re.search(r"#/calls/([^?#]+)", href or "")
    return urllib.parse.unquote(match.group(1)) if match else None


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
        page.goto(url)
        page.get_by_label(re.compile("^(Username|Email)$")).first.fill(username)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_load_state("networkidle")
        page.goto(url.rstrip("/") + "/#/stream-audio")
        switch = page.get_by_role("checkbox", name=re.compile("Also record screen video"))
        switch.wait_for(timeout=30000)
        switch.check()
        page.get_by_label("Meeting Topic").fill(topic)
        page.get_by_role("button", name="Start Streaming").click()
        try:
            page.get_by_text("Screen video is ON").wait_for(timeout=45000)
            run.video_on_shown = True
            run.surface_text = page.get_by_text(re.compile("^Recording .* as video")).first.inner_text()
        except Exception as err:  # noqa: BLE001 — reported by the test, with the page's state
            run.errors.append(f"no 'Screen video is ON': {err}")
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
