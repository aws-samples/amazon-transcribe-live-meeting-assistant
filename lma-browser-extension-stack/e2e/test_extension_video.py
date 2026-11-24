# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The extension's screen video recorder, in headless Chromium. No AWS, no network.

The recorder page (``content_scripts/recorder/recorder.html``) is opened from the
staged extension, with ``getDisplayMedia`` answered by a fake capture source: a
canvas slideshow, changing slide every three seconds at five frames a second,
whose video track reports the surface the test chooses and the capture handle of
the tab it stands for (the label the recorder chose, for the meeting tab). The
page stands in for the meeting tab's content script too, answering the
recorder's request to label the tab. The extension's service
worker stands in for the side panel: it sends the recorder the messages the
panel sends (``StartTranscription`` with ``video``, ``StartVideo``,
``StopVideo``) and collects what the recorder hands back.

What it asserts is what the transcriber's video lane needs and what the Jest
tests cannot show, because they have no MediaRecorder: the share is asked for
with the video constraints and with tab switching off; the chunks form one
fragmented MP4 stream (initialization segment, then ``moof`` fragments) whose
H.264 key frames are at most two seconds apart; and a surface other than the
meeting tab records no video and says so.

The last tests use no fake: a meeting tab and another tab, the extension's own
content script in each, and Chromium's real tab capture, which a launch switch
answers with the meeting tab or with the other tab. They show that the label
check tells the two apart in a real browser.
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
  // Which tab the share stands for: 'meeting' carries the recorder's label.
  window.__shared = 'meeting';
  window.__label = null;
  // The meeting tab's content script, for a recorder opened as a page of its own.
  window.addEventListener('message', (event) => {
    if (!event.data || event.data.lma !== 'lma-set-capture-handle') return;
    window.__label = event.data.handle;
    window.postMessage({ lma: 'lma-capture-handle-set', handle: event.data.handle, ok: true }, '*');
  });
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
    video.getCaptureHandle = () => (window.__surface !== 'browser' ? null
      : { handle: window.__shared === 'meeting' ? window.__label : 'another-tabs-label' });
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
        context.route(
            "**/*",
            lambda route: (
                route.abort() if route.request.url.startswith(("http", "ws")) else route.continue_()
            ),
        )
        try:
            yield {"context": context, "extension_id": extension_id, "playwright": playwright}
        finally:
            context.close()


def _wait_for(target, expression: str, timeout_s: float = 15.0) -> None:
    """Poll ``expression`` from here: an extension page's CSP refuses the eval ``wait_for_function`` polls with."""
    deadline = time.monotonic() + timeout_s
    while not target.evaluate(expression):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {expression}")
        time.sleep(0.1)


def _record(browser, surface: str, seconds: float, shared: str = "meeting") -> dict:
    context = browser["context"]
    worker = eb.service_worker(context)
    worker.evaluate(COLLECT)
    recorder_url = (
        f"chrome-extension://{browser['extension_id']}/content_scripts/recorder/recorder.html"
    )
    page = context.new_page()
    page.add_init_script(FAKE_CAPTURE)
    try:
        page.goto(recorder_url)
        page.evaluate(f"window.__surface = {surface!r}; window.__shared = {shared!r}")
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StartTranscription', video: true })")
        _wait_for(page, "window.__gdmOptions !== null")
        time.sleep(1.5)
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StartVideo' })")
        time.sleep(seconds)
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StopVideo' })")
        time.sleep(2.0)
        options = page.evaluate("window.__gdmOptions")
        label = page.evaluate("window.__label")
        messages = worker.evaluate("self.__video")
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StopTranscription' })")
        return {"options": options, "messages": messages, "label": label}
    finally:
        page.close()


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    at = start
    while at + 8 <= end:
        size = int.from_bytes(data[at : at + 4], "big")
        kind = data[at + 4 : at + 8].decode("latin-1")
        header = 8
        if size == 1:
            size = int.from_bytes(data[at + 8 : at + 16], "big")
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
    timescale = int.from_bytes(data[mdhd[0] + (20 if data[mdhd[0]] == 1 else 12) :][:4], "big")
    trex = _child(data, *_child(data, *moov, "mvex"), "trex")
    default_flags = int.from_bytes(data[trex[0] + 20 : trex[0] + 24], "big")
    times = []
    for kind, s, e in top:
        if kind != "moof":
            continue
        traf = _child(data, s, e, "traf")
        tfhd = _child(data, *traf, "tfhd")
        tfdt = _child(data, *traf, "tfdt")
        tf = int.from_bytes(data[tfhd[0] + 1 : tfhd[0] + 4], "big")
        p = tfhd[0] + 8 + (8 if tf & 1 else 0) + (4 if tf & 2 else 0)
        duration = 0
        if tf & 8:
            duration = int.from_bytes(data[p : p + 4], "big")
            p += 4
        p += 4 if tf & 0x10 else 0
        flags = int.from_bytes(data[p : p + 4], "big") if tf & 0x20 else default_flags
        t = (
            int.from_bytes(data[tfdt[0] + 4 : tfdt[0] + 12], "big")
            if data[tfdt[0]] == 1
            else int.from_bytes(data[tfdt[0] + 4 : tfdt[0] + 8], "big")
        )
        for k, ts, te in _boxes(data, *traf):
            if k != "trun":
                continue
            rf = int.from_bytes(data[ts + 1 : ts + 4], "big")
            count = int.from_bytes(data[ts + 4 : ts + 8], "big")
            q = ts + 8 + (4 if rf & 1 else 0)
            first = None
            if rf & 4:
                first = int.from_bytes(data[q : q + 4], "big")
                q += 4
            for i in range(count):
                d, f = duration, (first if i == 0 and first is not None else flags)
                if rf & 0x100:
                    d = int.from_bytes(data[q : q + 4], "big")
                    q += 4
                q += 4 if rf & 0x200 else 0
                if rf & 0x400:
                    f = int.from_bytes(data[q : q + 4], "big")
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
    assert options["video"] == {
        "frameRate": {"ideal": 5, "max": 5},
        "width": {"max": 1920},
        "height": {"max": 1920},
    }


def test_the_meeting_tab_is_recorded_as_fragmented_mp4(meeting_tab) -> None:
    actions = [m["action"] for m in meeting_tab["messages"]]
    assert actions[0] == "VideoStarted", actions
    assert actions[-1] == "VideoStopped", actions
    assert meeting_tab["messages"][0]["mimeType"].startswith("video/mp4"), meeting_tab["messages"][
        0
    ]
    chunks = [
        base64.b64decode(m["data"].split(",", 1)[1])
        for m in meeting_tab["messages"]
        if m["action"] == "VideoChunk"
    ]
    assert len(chunks) >= 3, actions
    kinds = [k for k, _, _ in _boxes(b"".join(chunks))]
    assert kinds[:2] == ["ftyp", "moov"] and "moof" in kinds, kinds[:6]


def test_key_frames_are_at_most_two_seconds_apart(meeting_tab) -> None:
    data = b"".join(
        base64.b64decode(m["data"].split(",", 1)[1])
        for m in meeting_tab["messages"]
        if m["action"] == "VideoChunk"
    )
    times = keyframe_times(data)
    assert len(times) >= 5, times
    gaps = [round(b - a, 3) for a, b in itertools.pairwise(times)]
    # The transcriber samples key frames at least every two seconds; allow
    # for the timescale's rounding.
    assert max(gaps) <= 2.05, f"key frames at {times}"


def test_the_meeting_tab_is_labelled_before_the_share_is_asked_for(meeting_tab) -> None:
    assert meeting_tab["label"], meeting_tab


def test_a_screen_records_no_video(browser) -> None:
    result = _record(browser, "monitor", seconds=2)
    actions = [m["action"] for m in result["messages"]]
    assert actions == ["VideoUnavailable"], actions
    assert result["messages"][0]["reason"] == "not-meeting-tab"


def test_another_tab_records_no_video(browser) -> None:
    result = _record(browser, "browser", seconds=2, shared="another-tab")
    actions = [m["action"] for m in result["messages"]]
    assert actions == ["VideoUnavailable"], actions
    assert result["messages"][0]["reason"] == "not-meeting-tab"


def test_a_recorder_that_is_not_capturing_does_not_answer(browser) -> None:
    """A recorder that is not capturing ignores video messages, besides the panel sending them only to the captured tab."""
    context = browser["context"]
    worker = eb.service_worker(context)
    worker.evaluate(COLLECT)
    page = context.new_page()
    try:
        page.goto(
            f"chrome-extension://{browser['extension_id']}/content_scripts/recorder/recorder.html"
        )
        worker.evaluate("chrome.runtime.sendMessage({ action: 'StartVideo' })")
        time.sleep(1.5)
        assert worker.evaluate("self.__video") == []
    finally:
        page.close()


#: The two tabs of the real-capture tests, served by the test itself.
MEETING_URL = "https://meeting.example.test/"
OTHER_URL = "https://other.example.test/"
OTHER_TITLE = "Another tab"


#: For the share of another tab only: the recorder's share request without
#: ``preferCurrentTab``. With it, Chromium asks whether to share this tab, which
#: ``--auto-select-tab-capture-source-by-title`` does not answer; without it, the
#: switch picks the other tab. The capture and its capture handle stay real.
PLAIN_SHARE_PICKER = """
(() => {
  if (location.protocol !== 'chrome-extension:' || !navigator.mediaDevices) return;
  const real = navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getDisplayMedia = (options) => {
    const { preferCurrentTab, ...rest } = options || {};
    return real(rest);
  };
})();
"""


def _tab_page(route) -> None:
    title = "Meeting" if route.request.url.startswith(MEETING_URL) else OTHER_TITLE
    route.fulfill(content_type="text/html", body=f"<html><head><title>{title}</title></head><body><h1>{title}</h1></body></html>")


def _record_real_share(browser, tmp_path_factory, share_switch: str, *, plain_picker: bool = False) -> list[dict]:
    """Start a capture with video from the meeting tab's own recorder, Chromium answering the share with ``share_switch``."""
    root = tmp_path_factory.mktemp("extension-real-share")
    extension = eb.stage_extension(root / "extension", config=eb.OFFLINE_CONFIG)
    context, _extension_id = eb.launch(
        browser["playwright"],
        extension,
        root / "profile",
        extra_args=("--use-fake-device-for-media-stream", share_switch),
    )
    try:
        context.route("https://*.example.test/**", _tab_page)
        # The microphone, which the recorder asks for after the share, without a
        # prompt. --use-fake-ui-for-media-stream would also answer the share
        # itself, which is what the switch under test chooses.
        context.grant_permissions(["microphone"], origin=MEETING_URL.rstrip("/"))
        if plain_picker:
            context.add_init_script(PLAIN_SHARE_PICKER)
        worker = eb.service_worker(context)
        worker.evaluate(COLLECT)
        # Opened first, so the meeting tab is in front when it asks for the share.
        context.new_page().goto(OTHER_URL)
        meeting = context.new_page()
        meeting.goto(MEETING_URL)
        meeting.bring_to_front()
        # The recorder iframe the content script injects has loaded.
        _wait_for(meeting, "!!document.getElementById('permissionsIFrame')")
        time.sleep(1.0)
        # To the meeting tab only, as the side panel sends it.
        send = (
            "(async (message) => { const [tab] = await chrome.tabs.query({ url: '" + MEETING_URL + "*' });"
            " chrome.tabs.sendMessage(tab.id, message).catch(() => undefined); return tab.id; })"
        )
        worker.evaluate(send, {"action": "StartTranscription", "video": True})
        time.sleep(4.0)
        worker.evaluate(send, {"action": "StartVideo"})
        _wait_for(worker, "self.__video.length > 0", timeout_s=10)
        time.sleep(2.5)
        worker.evaluate(send, {"action": "StopVideo"})
        time.sleep(1.0)
        messages = worker.evaluate("self.__video")
        worker.evaluate(send, {"action": "StopTranscription"})
        return messages
    finally:
        context.close()


def test_a_real_share_of_the_meeting_tab_records_video(browser, tmp_path_factory) -> None:
    messages = _record_real_share(browser, tmp_path_factory, "--auto-accept-this-tab-capture")
    actions = [m["action"] for m in messages]
    assert actions[0] == "VideoStarted", actions


def test_a_real_share_of_another_tab_records_no_video(browser, tmp_path_factory) -> None:
    messages = _record_real_share(
        browser, tmp_path_factory, f"--auto-select-tab-capture-source-by-title={OTHER_TITLE}", plain_picker=True
    )
    actions = [m["action"] for m in messages]
    assert actions == ["VideoUnavailable"], actions
    assert messages[0]["reason"] == "not-meeting-tab"
