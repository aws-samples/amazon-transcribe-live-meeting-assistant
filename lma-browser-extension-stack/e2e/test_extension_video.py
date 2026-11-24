# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The extension's screen video recorder, in headless Chromium. No AWS, no network.

The recorder page (``content_scripts/recorder/recorder.html``) is opened from the
staged extension, with ``getDisplayMedia`` answered by a fake capture source: a
canvas slideshow, changing slide every three seconds at five frames a second,
whose video track reports the surface the test chooses. The extension's service
worker stands in for the side panel: it sends the recorder the messages the
panel sends (``StartTranscription`` with ``video``, ``StartVideo``,
``StopVideo``) and collects what the recorder hands back.

What it asserts is what the transcriber's video lane needs and what the Jest
tests cannot show, because they have no MediaRecorder: the share is asked for
with the video constraints and with tab switching off; the chunks form one
fragmented MP4 stream (initialization segment, then ``moof`` fragments) whose
H.264 key frames are at most two seconds apart; and a surface other than the
meeting tab records no video and says so.
"""

from __future__ import annotations

import base64
import itertools
import time

import extension_browser as eb
import pytest
from playwright import sync_api

#: Sent to the recorder page before its own script runs: the fake capture source.
FAKE_CAPTURE = """
(() => {
  window.__gdmOptions = null;
  window.__surface = 'browser';
  const slideshow = () => {
    const canvas = document.createElement('canvas');
    canvas.width = 1280; canvas.height = 720;
    const g = canvas.getContext('2d');
    let n = 0;
    setInterval(() => {
      n += 1;
      const slide = Math.floor(n / 15);
      g.fillStyle = ['#ffffff', '#202a6a', '#f0d040', '#1a5a30'][slide % 4];
      g.fillRect(0, 0, 1280, 720);
      g.fillStyle = slide % 2 ? '#ffffff' : '#101010';
      g.font = 'bold 96px sans-serif';
      g.fillText('Slide ' + (slide + 1), 80, 200);
      g.fillRect((n * 7) % 1200, 640, 20, 20);
    }, 200);
    return canvas;
  };
  navigator.mediaDevices.getDisplayMedia = async (options) => {
    window.__gdmOptions = JSON.parse(JSON.stringify(options));
    const video = slideshow().captureStream(5).getVideoTracks()[0];
    const settings = video.getSettings.bind(video);
    video.getSettings = () => ({ ...settings(), displaySurface: window.__surface });
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    return new MediaStream([video, ...mic.getAudioTracks()]);
  };
})();
"""

#: Installed in the service worker: collect what the recorder sends the panel.
COLLECT = """
(() => {
  self.__video = [];
  if (self.__collecting) return true;
  self.__collecting = true;
  chrome.runtime.onMessage.addListener((message) => {
    if (message && typeof message.action === 'string' && message.action.startsWith('Video')) {
      self.__video.push(message);
    }
  });
  return true;
})()
"""


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    root = tmp_path_factory.mktemp("extension-video-e2e")
    extension = eb.stage_extension(root / "extension", config=eb.OFFLINE_CONFIG)
    with sync_api.sync_playwright() as playwright:
        context, extension_id = eb.launch(
            playwright,
            extension,
            root / "profile",
            extra_args=("--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"),
        )
        # Nothing here needs the network.
        context.route("**/*", lambda route: route.abort() if route.request.url.startswith(("http", "ws")) else route.continue_())
        try:
            yield {"context": context, "extension_id": extension_id}
        finally:
            context.close()


def _record(browser, surface: str, seconds: float) -> dict:
    context = browser["context"]
    worker = eb.service_worker(context)
    worker.evaluate(COLLECT)
    page = context.new_page()
    page.add_init_script(FAKE_CAPTURE)
    try:
        page.goto(f"chrome-extension://{browser['extension_id']}/content_scripts/recorder/recorder.html")
        page.evaluate(f"window.__surface = {surface!r}")
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StartTranscription', video: true })")
        page.wait_for_function("window.__gdmOptions !== null", timeout=15_000)
        time.sleep(1.5)
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StartVideo' })")
        time.sleep(seconds)
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StopVideo' })")
        time.sleep(2.0)
        options = page.evaluate("window.__gdmOptions")
        messages = worker.evaluate("self.__video")
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StopTranscription' })")
        return {"options": options, "messages": messages}
    finally:
        page.close()


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    at = start
    while at + 8 <= end:
        size = int.from_bytes(data[at:at + 4], "big")
        kind = data[at + 4:at + 8].decode("latin-1")
        header = 8
        if size == 1:
            size = int.from_bytes(data[at + 8:at + 16], "big")
            header = 16
        if size < header or at + size > end:
            return
        yield kind, at + header, at + size
        at += size


def _child(data: bytes, start: int, end: int, kind: str):
    return next(((s, e) for k, s, e in _boxes(data, start, end) if k == kind), None)


def keyframe_times(data: bytes) -> list[float]:
    """The sync sample times of a fragmented MP4, in seconds (moov timescale, trex/tfhd/trun flags)."""
    top = list(_boxes(data))
    moov = next((s, e) for k, s, e in top if k == "moov")
    trak = _child(data, *moov, "trak")
    mdhd = _child(data, *_child(data, *trak, "mdia"), "mdhd")
    timescale = int.from_bytes(data[mdhd[0] + (20 if data[mdhd[0]] == 1 else 12):][:4], "big")
    trex = _child(data, *_child(data, *moov, "mvex"), "trex")
    default_flags = int.from_bytes(data[trex[0] + 20:trex[0] + 24], "big")
    times = []
    for kind, s, e in top:
        if kind != "moof":
            continue
        traf = _child(data, s, e, "traf")
        tfhd = _child(data, *traf, "tfhd")
        tfdt = _child(data, *traf, "tfdt")
        tf = int.from_bytes(data[tfhd[0] + 1:tfhd[0] + 4], "big")
        p = tfhd[0] + 8 + (8 if tf & 1 else 0) + (4 if tf & 2 else 0)
        duration = 0
        if tf & 8:
            duration = int.from_bytes(data[p:p + 4], "big")
            p += 4
        p += 4 if tf & 0x10 else 0
        flags = int.from_bytes(data[p:p + 4], "big") if tf & 0x20 else default_flags
        t = (int.from_bytes(data[tfdt[0] + 4:tfdt[0] + 12], "big") if data[tfdt[0]] == 1
             else int.from_bytes(data[tfdt[0] + 4:tfdt[0] + 8], "big"))
        for k, ts, te in _boxes(data, *traf):
            if k != "trun":
                continue
            rf = int.from_bytes(data[ts + 1:ts + 4], "big")
            count = int.from_bytes(data[ts + 4:ts + 8], "big")
            q = ts + 8 + (4 if rf & 1 else 0)
            first = None
            if rf & 4:
                first = int.from_bytes(data[q:q + 4], "big")
                q += 4
            for i in range(count):
                d, f = duration, (first if i == 0 and first is not None else flags)
                if rf & 0x100:
                    d = int.from_bytes(data[q:q + 4], "big")
                    q += 4
                q += 4 if rf & 0x200 else 0
                if rf & 0x400:
                    f = int.from_bytes(data[q:q + 4], "big")
                    q += 4
                q += 4 if rf & 0x800 else 0
                if not f & 0x10000:
                    times.append(t / timescale)
                t += d
    return times


@pytest.fixture(scope="module")
def meeting_tab(browser) -> dict:
    return _record(browser, "browser", seconds=14)


def test_the_share_is_asked_for_with_the_video_constraints(meeting_tab) -> None:
    options = meeting_tab["options"]
    assert options["preferCurrentTab"] is True
    assert options["surfaceSwitching"] == "exclude"
    assert options["video"] == {"frameRate": {"ideal": 5, "max": 5}, "width": {"max": 1920}, "height": {"max": 1920}}


def test_the_meeting_tab_is_recorded_as_fragmented_mp4(meeting_tab) -> None:
    actions = [m["action"] for m in meeting_tab["messages"]]
    assert actions[0] == "VideoStarted", actions
    assert actions[-1] == "VideoStopped", actions
    assert meeting_tab["messages"][0]["mimeType"].startswith("video/mp4"), meeting_tab["messages"][0]
    chunks = [base64.b64decode(m["data"].split(",", 1)[1]) for m in meeting_tab["messages"] if m["action"] == "VideoChunk"]
    assert len(chunks) >= 3, actions
    kinds = [k for k, _, _ in _boxes(b"".join(chunks))]
    assert kinds[:2] == ["ftyp", "moov"] and "moof" in kinds, kinds[:6]


def test_key_frames_are_at_most_two_seconds_apart(meeting_tab) -> None:
    data = b"".join(
        base64.b64decode(m["data"].split(",", 1)[1]) for m in meeting_tab["messages"] if m["action"] == "VideoChunk"
    )
    times = keyframe_times(data)
    assert len(times) >= 5, times
    gaps = [round(b - a, 3) for a, b in itertools.pairwise(times)]
    # The transcriber samples key frames at least every two seconds; allow
    # for the timescale's rounding.
    assert max(gaps) <= 2.05, f"key frames at {times}"


def test_a_share_other_than_the_meeting_tab_records_no_video(browser) -> None:
    result = _record(browser, "monitor", seconds=2)
    actions = [m["action"] for m in result["messages"]]
    assert actions == ["VideoUnavailable"], actions
    assert result["messages"][0]["reason"] == "surface"
