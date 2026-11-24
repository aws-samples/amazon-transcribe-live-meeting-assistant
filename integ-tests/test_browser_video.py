# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Screen video from the Stream Audio/Video page and the browser extension, on a live stack.

Streams the narrated slideshow of ``desktop_video_probe.py`` as each browser
client would (``clientType`` ``STREAM_AUDIO`` and ``BROWSER_EXTENSION``), over
the same second connection the Desktop Capture App uses, and checks what the
stack stored and refused (see browser_video_probe.py):

1. With Meeting Recording › Record screen video from Stream Audio/Video and the
   browser extension on, the settings answer allows video, and each client's
   meeting gets one key frame per slide under its own ``Source``, a video
   recording and its capture source.
2. A meeting continued for a second session records each session's video, and
   places the second session's key frames after the first's.
3. With an opt-in consent mode, a meeting refused for its consent admits no
   video; one whose consent is granted does once the grant arrives. With notify,
   an objection closes the video connection and ends the meeting, and the video
   recorded until then is kept.
4. With the setting off, the settings answer says so and ``START_VIDEO`` from
   either client is refused while the audio carries on.

The test changes two deployment-wide settings, Meeting Recording and Meeting
Consent, through the configuration store, and always puts them back (see
meeting_consent_probe.ConsentSettingsOverride).

Opt-in. It runs only with ``LMA_TEST_BROWSER_VIDEO=1``, because it takes about
eight minutes, calls Polly, and needs a stack whose transcriber has browser
video and records video (``EnableVideoRecording=true``):

    AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \\
      make integ-tests STACK=lma-integtest1 LMA_TEST_BROWSER_VIDEO=1

Once opted in, a missing dependency or test user is a failure, not a skip. The
tests are marked ``optional_feature`` so that a run without the flag, the
scheduled one included, reports them as skipped rather than failed. The
continued meeting needs ``reopenMeeting``, which accepts an assumed role over
IAM: with an IAM user's keys those two tests are skipped, saying so.
"""

from __future__ import annotations

import importlib
import os
import time

import cognito_test_user
import pytest

ENABLED = os.environ.get("LMA_TEST_BROWSER_VIDEO", "") not in ("", "0", "false")
# As in test_desktop_video.py: one key frame interval either way, plus half a
# second for the video connection opening after the audio.
TIMESTAMP_TOLERANCE_SECONDS = 2.5
# Two slides' worth of video for the shorter legs (each slide is held 8 s).
TWO_SLIDES = 8
NOT_ENABLED = "screen video is not enabled for this client"
CONSENT_CLOSE = "meeting consent does not allow capture"
NOTICE = "Integration Test would like to record and transcribe this meeting (Browser video probe)."

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_BROWSER_VIDEO=1"),
]


def _require_dependencies() -> None:
    missing = []
    for module, requirement in (("pycognito", "requirements.txt"), ("websockets", "requirements.txt"),
                                ("PIL", "requirements-video.txt")):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(f"{module} (pip install -r integ-tests/{requirement})")
    if missing:
        pytest.fail("LMA_TEST_BROWSER_VIDEO is set but these are missing: " + ", ".join(missing))
    from desktop_video_probe import find_ffmpeg

    try:
        find_ffmpeg()
    except FileNotFoundError as err:
        pytest.fail(str(err))


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def stack(client):
    _require_dependencies()
    if not cognito_test_user.available(client.region):
        pytest.fail(f"LMA_TEST_BROWSER_VIDEO is set but there is no Cognito test user — "
                    f"{cognito_test_user.describe_sources()}")
    from meeting_consent_probe import Stack

    return Stack.find(client.stack_name, client.region)


@pytest.fixture(scope="module")
def user(stack):
    from meeting_consent_probe import tokens_for_test_user

    username, tokens = tokens_for_test_user(stack)
    return {"username": username, "tokens": tokens}


@pytest.fixture(scope="module")
def media(stack):
    from desktop_video_probe import build_media

    return build_media(stack.region)


def _answer(stack, user, client_type: str) -> dict:
    from browser_video_probe import settings_answer

    answer = settings_answer(stack.outputs["LMAWebsocketEndpoint"], user["tokens"], client_type)
    if answer is None or "videoAllowed" not in answer:
        pytest.fail(
            f"the deployed transcriber's settings answer has no videoAllowed ({answer}); "
            "deploy a version with browser video"
        )
    return answer


def _wait_for_video_allowed(stack, user, allowed: bool) -> dict:
    """Poll the settings answer until it reports ``allowed`` for both browser clients."""
    deadline = time.monotonic() + 30
    answers: dict = {}
    while time.monotonic() < deadline:
        answers = {c: _answer(stack, user, c)["videoAllowed"] for c in ("STREAM_AUDIO", "BROWSER_EXTENSION")}
        if set(answers.values()) == {allowed}:
            return answers
        time.sleep(2)
    return answers


@pytest.fixture(scope="module")
def config(stack, user):
    """Meeting Recording and Meeting Consent, saved first and always put back.

    Both sections are changed on the same profile: Meeting Recording through
    the override's own save (which creates a temporary profile on a read-only
    one), Meeting Consent attached to it. They are restored in reverse order.
    """
    from meeting_consent_probe import ConsentSettingsOverride

    _answer(stack, user, "STREAM_AUDIO")  # fails here, nothing changed, on an older transcriber
    recording = ConsentSettingsOverride(stack, section="recording")
    recording.save()
    consent = ConsentSettingsOverride(store=recording.store, section="meeting_consent")
    record = recording.store.read_profile(recording.profile, consistent_read=True)
    consent.profile = recording.profile
    consent.had_section = "meeting_consent" in record.body
    consent.saved = dict(record.body.get("meeting_consent") or {}) if consent.had_section else None
    try:
        recording.apply(recordBrowserVideo=True)
        # Off, so legs without a consent step are accepted.
        _set_consent({"consent": consent}, stack, user, mode="off")
        answers = _wait_for_video_allowed(stack, user, True)
        if set(answers.values()) != {True}:
            pytest.fail(
                f"Record screen video from Stream Audio/Video and the browser extension is on, but the "
                f"transcriber reports videoAllowed {answers}: the stack does not record video "
                "(EnableVideoRecording=false)"
            )
        yield {"recording": recording, "consent": consent}
    finally:
        consent.restore()
        recording.restore()


def _set_consent(config, stack, user, **values) -> None:
    """Write Meeting Consent and wait until the transcriber's settings answer reports it."""
    config["consent"].apply(**values)
    answer: dict = {}
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        answer = _answer(stack, user, "STREAM_AUDIO")
        if answer.get("available") and answer.get("mode") == values["mode"]:
            return
        time.sleep(2)
    pytest.fail(f"saved meeting consent {values} but the transcriber reports {answer}")


def _stream(stack, user, media, **kwargs):
    from browser_video_probe import stream_leg

    leg = stream_leg(stack.outputs["LMAWebsocketEndpoint"], user["tokens"], user["username"], media, **kwargs)
    print(leg.summary())
    return leg


def _keyframes(stack, call_id: str) -> list:
    from desktop_video_probe import read_keyframes

    return read_keyframes(stack.session.client("dynamodb"), stack.event_table, call_id)


def _collect(stack, call_id: str, want_frames: int, want_video: bool, timeout: float = 150.0) -> dict:
    """The meeting record and key frames once they are all there, or as they are at the timeout."""
    from meeting_consent_probe import read_meeting

    deadline = time.monotonic() + timeout
    while True:
        frames = _keyframes(stack, call_id)
        meeting = read_meeting(stack, call_id) or {}
        done = len(frames) >= want_frames and (not want_video or meeting.get("VideoRecordingUrl"))
        if done or time.monotonic() >= deadline:
            if done:
                time.sleep(5)  # a frame more would be a detector finding
                frames = _keyframes(stack, call_id)
            return {"frames": frames, "meeting": meeting}
        time.sleep(5)


def _summary(frames: list) -> str:
    return ", ".join(f"{f.get('FrameId')}@{f.get('Timestamp')}({f.get('Source')})" for f in frames) or "none"


def _assert_one_frame_per_slide(frames: list, expected: list, source: str) -> None:
    assert len(frames) == len(expected), (
        f"expected {len(expected)} key frames (one per slide, at {expected}), got {len(frames)}: {_summary(frames)}"
    )
    assert {f.get("Source") for f in frames} == {source}, _summary(frames)
    stamps = [f["Timestamp"] for f in frames]
    assert stamps == sorted(stamps), f"key frame timestamps are not in order: {stamps}"
    for want, got in zip(expected, stamps):
        assert abs(got - want) <= TIMESTAMP_TOLERANCE_SECONDS, f"a slide at {want}s has its key frame at {got}s"


# ── 1. the two clients, setting on ─────────────────────────────────────────


def test_the_settings_answer_allows_video_from_both_browser_clients(stack, user, config) -> None:
    for client_type in ("STREAM_AUDIO", "BROWSER_EXTENSION", "DESKTOP_APP"):
        assert _answer(stack, user, client_type)["videoAllowed"] is True, client_type


@pytest.fixture(scope="module")
def stream_page(stack, user, media, config):
    from browser_video_probe import expected_timestamps, new_call_id

    leg = _stream(stack, user, media, client_type="STREAM_AUDIO", call_id=new_call_id("stream-page"))
    expected = expected_timestamps(media, leg)
    return {"leg": leg, "expected": expected, **_collect(stack, leg.call_id, len(expected), True)}


def test_stream_page_video_streams_alongside_audio(stream_page) -> None:
    leg = stream_page["leg"]
    assert leg.video_state == "OPEN" and leg.end_video_sent, leg.summary()
    assert leg.audio_state == "OPEN" and leg.end_sent, leg.summary()
    assert leg.fragments_sent == leg.fragments_planned > 0, leg.summary()


def test_stream_page_video_keeps_one_key_frame_per_slide_under_its_source(stream_page) -> None:
    _assert_one_frame_per_slide(stream_page["frames"], stream_page["expected"], "STREAM_AUDIO")


def test_stream_page_video_is_recorded_with_the_meeting(stream_page) -> None:
    meeting = stream_page["meeting"]
    assert meeting.get("CaptureSource") == "STREAM_AUDIO", meeting.get("CaptureSource")
    assert meeting.get("VideoRecordingUrl"), f"no video recording on {stream_page['leg'].call_id}"


@pytest.fixture(scope="module")
def extension(stack, user, media, config):
    from browser_video_probe import expected_timestamps, new_call_id

    leg = _stream(stack, user, media, client_type="BROWSER_EXTENSION", call_id=new_call_id("extension"),
                  fragments=TWO_SLIDES)
    expected = expected_timestamps(media, leg)
    return {"leg": leg, "expected": expected, **_collect(stack, leg.call_id, len(expected), True)}


def test_extension_video_keeps_one_key_frame_per_slide_under_its_source(extension) -> None:
    leg = extension["leg"]
    assert leg.video_state == "OPEN" and leg.end_video_sent, leg.summary()
    _assert_one_frame_per_slide(extension["frames"], extension["expected"], "BROWSER_EXTENSION")


def test_extension_video_is_recorded_with_the_meeting(extension) -> None:
    meeting = extension["meeting"]
    assert meeting.get("CaptureSource") == "BROWSER_EXTENSION", meeting.get("CaptureSource")
    assert meeting.get("VideoRecordingUrl"), f"no video recording on {extension['leg'].call_id}"


# ── 2. a continued meeting ─────────────────────────────────────────────────


@pytest.fixture(scope="module")
def continued(client, stack, user, media, config):
    from browser_video_probe import expected_timestamps, new_call_id
    from meeting_consent_probe import read_meeting, wait_for

    if ":assumed-role/" not in client.session.client("sts").get_caller_identity().get("Arn", ""):
        pytest.skip("reopenMeeting accepts an assumed role over IAM; these credentials are an IAM user's")
    call_id = new_call_id("continued")
    first = _stream(stack, user, media, client_type="STREAM_AUDIO", call_id=call_id, fragments=TWO_SLIDES)
    ended = wait_for(lambda: (read_meeting(stack, call_id) or {}).get("Status") == "ENDED", timeout=120)
    assert ended, f"the first session of {call_id} did not end: {read_meeting(stack, call_id)}"
    reopened = client.meetings.reopen(call_id)
    second = _stream(stack, user, media, client_type="STREAM_AUDIO", call_id=call_id,
                     session_number=reopened.session_number, fragments=TWO_SLIDES)
    expected_first = expected_timestamps(media, first)
    expected_second = expected_timestamps(media, second, reopened.session_offset_seconds)
    collected = _collect(stack, call_id, len(expected_first) + len(expected_second), True)
    collected["meeting"] = wait_for(
        lambda: (m := read_meeting(stack, call_id) or {}) and len(m.get("VideoRecordingUrls") or []) >= 2 and m,
        timeout=120,
    ) or read_meeting(stack, call_id) or {}
    return {"first": first, "second": second, "reopened": reopened,
            "expected": expected_first + expected_second, **collected}


def test_continued_meeting_records_each_sessions_video(continued) -> None:
    for leg in (continued["first"], continued["second"]):
        assert leg.video_state == "OPEN" and leg.end_video_sent, leg.summary()
    meeting = continued["meeting"]
    sessions = meeting.get("VideoRecordingUrlSessions") or []
    assert sorted(sessions) == [1, continued["reopened"].session_number], (
        f"video recordings by session: {sessions}, {meeting.get('VideoRecordingUrls')}"
    )


def test_continued_meeting_places_the_later_sessions_key_frames_after_the_earlier(continued) -> None:
    _assert_one_frame_per_slide(continued["frames"], continued["expected"], "STREAM_AUDIO")
    offset = continued["reopened"].session_offset_seconds
    later = [f for f in continued["frames"] if f["Timestamp"] >= offset]
    assert len(later) == len(continued["expected"]) // 2, (
        f"the second session starts at {offset}s; key frames: {_summary(continued['frames'])}"
    )


# ── 3. consent ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def refused(stack, user, media, config):
    from browser_video_probe import new_call_id
    from meeting_consent_probe import read_meeting

    _set_consent(config, stack, user, mode="opt_in", consentRule="owner_attests")
    leg = _stream(stack, user, media, client_type="STREAM_AUDIO", call_id=new_call_id("refused"),
                  fragments=3, consent={"mode": "opt_in", "confirmed": False, "noticeText": NOTICE,
                                        "client": "LMA integration test"})
    time.sleep(20)
    return {"leg": leg, "frames": _keyframes(stack, leg.call_id), "meeting": read_meeting(stack, leg.call_id)}


def test_a_meeting_refused_for_its_consent_admits_no_video(refused) -> None:
    leg = refused["leg"]
    assert "CONSENT_NOT_CONFIRMED" in leg.codes(), leg.summary()
    if leg.video_started:
        assert leg.video_close_code == 1008, leg.summary()
    assert refused["frames"] == [], _summary(refused["frames"])
    assert refused["meeting"] is None, refused["meeting"]


@pytest.fixture(scope="module")
def granted(stack, user, media, config, refused):
    """After ``refused``, so both opt-in meetings run before the mode changes."""
    from browser_video_probe import new_call_id

    leg = _stream(stack, user, media, client_type="STREAM_AUDIO", call_id=new_call_id("granted"),
                  fragments=TWO_SLIDES, wait_for_grant=True,
                  consent={"mode": "opt_in", "confirmed": True, "method": "verbal", "noticeText": NOTICE,
                           "statement": "Everyone agreed at the start.", "client": "LMA integration test"})
    return {"leg": leg, **_collect(stack, leg.call_id, 1, True)}


def test_video_is_admitted_once_consent_is_granted(granted) -> None:
    leg = granted["leg"]
    assert leg.granted_at is not None, f"no CONSENT_STATE granted: {leg.summary()}"
    assert leg.video_state == "OPEN" and leg.end_video_sent, leg.summary()
    assert granted["frames"], f"no key frames for {leg.call_id}"
    assert {f.get("Source") for f in granted["frames"]} == {"STREAM_AUDIO"}, _summary(granted["frames"])


@pytest.fixture(scope="module")
def objected(stack, user, media, config, granted):
    from browser_video_probe import new_call_id
    from meeting_consent_probe import read_meeting, wait_for

    _set_consent(config, stack, user, mode="notify")
    leg = _stream(stack, user, media, client_type="BROWSER_EXTENSION", call_id=new_call_id("objected"),
                  fragments=TWO_SLIDES + 4, objection_after_fragments=TWO_SLIDES,
                  consent={"mode": "notify", "confirmed": True, "method": "chat", "noticeText": NOTICE,
                           "client": "LMA integration test"})
    meeting = wait_for(
        lambda: (m := read_meeting(stack, leg.call_id) or {}) and m.get("Status") == "ENDED"
        and m.get("VideoRecordingUrl") and m,
        timeout=150,
    ) or read_meeting(stack, leg.call_id) or {}
    return {"leg": leg, "meeting": meeting}


def test_an_objection_closes_the_video_connection(objected) -> None:
    leg = objected["leg"]
    assert leg.objection_sent_at is not None, leg.summary()
    assert "OBJECTION_RECEIVED" in leg.codes(), leg.summary()
    assert leg.video_close_code == 1008 and leg.video_close_reason == CONSENT_CLOSE, leg.summary()
    assert leg.video_closed_at - leg.objection_sent_at < 10, leg.summary()
    assert not leg.end_video_sent, leg.summary()


def test_an_objection_ends_the_meeting_and_keeps_the_video_so_far(objected) -> None:
    meeting = objected["meeting"]
    assert meeting.get("Status") == "ENDED", meeting.get("Status")
    assert (meeting.get("Consent") or {}).get("Status") == "objected", meeting.get("Consent")
    assert meeting.get("VideoRecordingUrl"), f"no video recording on {objected['leg'].call_id}"


# ── 4. setting off ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def setting_off(stack, user, media, config, objected):
    """Last, after every leg that needs the setting on."""
    from browser_video_probe import new_call_id

    _set_consent(config, stack, user, mode="off")
    config["recording"].apply(recordBrowserVideo=False)
    answers = _wait_for_video_allowed(stack, user, False)
    legs = {
        client_type: _stream(stack, user, media, client_type=client_type, call_id=new_call_id(f"off-{client_type}"),
                             fragments=3)
        for client_type in ("STREAM_AUDIO", "BROWSER_EXTENSION")
    }
    time.sleep(15)
    return {"answers": answers, "desktop": _answer(stack, user, "DESKTOP_APP")["videoAllowed"], "legs": legs,
            "frames": {c: _keyframes(stack, leg.call_id) for c, leg in legs.items()}}


def test_with_the_setting_off_the_settings_answer_withholds_browser_video(setting_off) -> None:
    assert setting_off["answers"] == {"STREAM_AUDIO": False, "BROWSER_EXTENSION": False}, setting_off["answers"]
    assert setting_off["desktop"] is True, "the Desktop Capture App follows the stack parameter alone"


def test_with_the_setting_off_browser_video_is_refused_and_the_audio_carries_on(setting_off) -> None:
    for client_type, leg in setting_off["legs"].items():
        assert leg.video_close_code == 1008 and leg.video_close_reason == NOT_ENABLED, leg.summary()
        assert leg.audio_state == "OPEN" and leg.end_sent, f"{client_type}: {leg.summary()}"
        assert setting_off["frames"][client_type] == [], _summary(setting_off["frames"][client_type])
