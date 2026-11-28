# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for scripts/synthetic_meetings (no AWS: Polly, Kinesis, DynamoDB, S3 and the
WebSocket are fakes)."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import re
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KIT_DIR = ROOT / "scripts" / "synthetic_meetings"
sys.path.insert(0, str(KIT_DIR))

import delivery  # noqa: E402
import kit  # noqa: E402
import run_meeting  # noqa: E402

pytestmark = pytest.mark.unit

SCHEMA = ROOT / "lma-ai-stack/source/appsync/schema.graphql"
CHANNELS_MODULE = (
    ROOT / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_channels/__init__.py"
)
TRANSCRIBER_EVENT_TYPES = (
    ROOT / "lma-websocket-transcriber-stack/source/app/src/calleventdata/eventtypes.ts"
)
BUNDLED = [p.stem for p in kit.bundled_meetings()]


def _meeting(script: list[dict], speakers: list[dict] | None = None, **top) -> kit.Meeting:
    data = {
        "title": "Test meeting",
        "speakers": speakers
        or [
            {"name": "Ann", "channel": "LOCAL", "voice": "Joanna"},
            {"name": "Bob", "channel": "REMOTE", "voice": "Matthew"},
            {"name": "Cat", "channel": "REMOTE", "voice": "Amy"},
        ],
        "script": script,
        **top,
    }
    return kit.parse_meeting(data, "test")


WORDS_10 = "one two three four five six seven eight nine ten"


# --- the bundled scripts ---------------------------------------------------------


def test_the_six_meetings_the_kit_documents_are_bundled() -> None:
    assert set(BUNDLED) >= {
        "sprint_planning",
        "architecture_review",
        "customer_discovery",
        "incident_review",
        "retrospective",
        "adversarial",
    }


@pytest.mark.parametrize("name", BUNDLED)
def test_every_bundled_meeting_loads_and_is_usable_over_both_paths(name: str) -> None:
    meeting = kit.find_meeting(name)
    assert kit.warnings(meeting) == [], (
        "short turns or unknown voices make the audio path unreliable"
    )
    minutes = kit.build_timeline(meeting)[-1].end / 60
    assert 2.5 <= minutes <= 8.5, f"{name} is about {minutes:.1f} minutes of speech"
    channels = {s.channel for s in meeting.speakers}
    assert channels == {"LOCAL", "REMOTE"}
    assert sum(s.channel == "REMOTE" for s in meeting.speakers) >= 2, (
        "a diarized channel needs two voices"
    )
    spoken = {t.name for t in meeting.turns}
    assert spoken == {s.name for s in meeting.speakers}, "every listed speaker speaks"
    assert meeting.chats, "every meeting exercises the meeting chat"
    assert meeting.agenda and meeting.meeting_context and meeting.purpose
    assert len({s.voice for s in meeting.speakers}) == len(meeting.speakers), (
        "one voice per speaker"
    )


def test_the_adversarial_meeting_has_its_hard_material() -> None:
    meeting = kit.find_meeting("adversarial")
    assert any(t.text.startswith("OK Assistant") and "ignore" in t.text for t in meeting.turns)
    assert any(
        "disregard" in c.text.lower() and "assistant" in c.text.lower() for c in meeting.chats
    )
    assert max(len(t.text.split()) for t in meeting.turns) > 250, "one very long rambling turn"
    assert any(
        t.text.lower().startswith("someone") or " someone " in t.text.lower() for t in meeting.turns
    )
    # The long turn is longer than kit.POLLY_TEXT_LIMIT, so it is split.
    longest = max(meeting.turns, key=lambda t: len(t.text))
    assert len(kit.split_for_polly(longest.text)) >= 2


def test_bundled_scripts_name_no_real_looking_identifiers() -> None:
    for path in kit.bundled_meetings():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d{12}\b", text), (
            f"{path.name}: a 12-digit number reads as an account id"
        )
        assert "arn:" not in text and "amazon" not in text.lower(), path.name


# --- parsing ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"speakers": [{"name": "Ann", "channel": "CALLER", "voice": "Joanna"}]}, "channel"),
        ({"script": [{"speaker": "Zed", "text": WORDS_10}]}, "not in speakers"),
        ({"script": [{"speaker": "Ann", "chat": "Ann", "text": WORDS_10}]}, "exactly one"),
        ({"script": [{"chat": "Ann", "text": "hi there"}]}, "at least one spoken turn"),
        ({"script": [{"speaker": "Ann", "text": WORDS_10, "pause": -1}]}, "pause"),
        ({"title": "Sprint #4"}, "title"),
        ({"colour": "blue"}, "unknown top-level"),
    ],
)
def test_a_malformed_script_is_refused_with_the_reason(change: dict, message: str) -> None:
    data = {
        "title": "T",
        "speakers": [{"name": "Ann", "channel": "LOCAL", "voice": "Joanna"}],
        "script": [{"speaker": "Ann", "text": WORDS_10}],
        **change,
    }
    with pytest.raises(kit.ScriptError, match=message):
        kit.parse_meeting(data, "bad")


def test_a_short_turn_and_an_unknown_voice_are_warned_about() -> None:
    meeting = _meeting(
        [{"speaker": "Ann", "text": "Yes."}],
        speakers=[{"name": "Ann", "channel": "LOCAL", "voice": "Jonna"}],
    )
    found = kit.warnings(meeting)
    assert any("Jonna" in w for w in found) and any("diarization" in w for w in found)


def test_a_script_is_found_by_path_as_well_as_by_name(tmp_path: Path) -> None:
    path = tmp_path / "mine.json"
    path.write_text(
        json.dumps(
            {
                "title": "Mine",
                "speakers": [{"name": "A", "channel": "LOCAL", "voice": "Ruth"}],
                "script": [{"speaker": "A", "text": WORDS_10}],
            }
        )
    )
    assert kit.find_meeting(str(path)).name == "mine"
    with pytest.raises(kit.ScriptError, match="not a bundled meeting"):
        kit.find_meeting("no_such_meeting")


# --- the timeline and pacing --------------------------------------------------------


def test_turns_follow_one_another_and_a_chat_message_takes_no_time() -> None:
    meeting = _meeting(
        [
            {"speaker": "Ann", "text": WORDS_10},
            {"chat": "Bob", "text": "a link", "pause": 2.0},
            {"speaker": "Bob", "text": WORDS_10, "pause": 1.5},
            {"speaker": "Cat", "text": WORDS_10},
        ]
    )
    first, chat, second, third = sorted(kit.build_timeline(meeting), key=lambda t: t.index)
    length = kit.estimate_seconds(WORDS_10)
    assert first.start == kit.LEAD_IN_SECONDS and first.end == pytest.approx(first.start + length)
    assert chat.channel == kit.CHAT_CHANNEL and chat.start == chat.end == pytest.approx(
        first.end + 2.0
    )
    assert second.start == pytest.approx(first.end + 1.5), "the chat does not push the next turn"
    assert third.start == pytest.approx(second.end + kit.DEFAULT_TURN_PAUSE)
    # In time order: the chat (typed 2 s after Ann) comes after Bob starts speaking (1.5 s).
    assert [t.index for t in kit.build_timeline(meeting)] == [0, 2, 1, 3]
    # Delivered when finished: Bob's turn is sent after the chat message.
    assert [t.index for t in kit.delivery_order(kit.build_timeline(meeting))] == [0, 1, 2, 3]


def test_measured_durations_replace_the_estimates() -> None:
    meeting = _meeting([{"speaker": "Ann", "text": WORDS_10}, {"speaker": "Bob", "text": WORDS_10}])
    timed = kit.build_timeline(meeting, {0: 7.25, 1: 3.0})
    assert timed[0].end - timed[0].start == pytest.approx(7.25)
    assert timed[1].start == pytest.approx(timed[0].end + kit.DEFAULT_TURN_PAUSE)
    assert timed[1].end - timed[1].start == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("90", kit.StopAfter(seconds=90.0)),
        ("90s", kit.StopAfter(seconds=90.0)),
        ("12.5", kit.StopAfter(seconds=12.5)),
        ("5turns", kit.StopAfter(turns=5)),
        ("5 turns", kit.StopAfter(turns=5)),
        ("3t", kit.StopAfter(turns=3)),
    ],
)
def test_stop_after_reads_seconds_or_turns(value: str, expected: kit.StopAfter) -> None:
    assert kit.parse_stop_after(value) == expected


@pytest.mark.parametrize("value", ["", "soon", "0turns", "-3"])
def test_stop_after_refuses_what_it_cannot_read(value: str) -> None:
    with pytest.raises(ValueError):
        kit.parse_stop_after(value)


def test_truncation_keeps_what_is_delivered_before_the_stop() -> None:
    meeting = _meeting(
        [
            {"speaker": "Ann", "text": WORDS_10},
            {"chat": "Bob", "text": "typed", "pause": 0.5},
            {"speaker": "Bob", "text": WORDS_10},
            {"speaker": "Cat", "text": WORDS_10},
        ]
    )
    timeline = kit.build_timeline(meeting)
    kept, cutoff = kit.truncate(timeline, kit.StopAfter(turns=2))
    assert [t.index for t in kept] == [0, 1, 2] and cutoff == max(t.end for t in kept)
    kept, cutoff = kit.truncate(timeline, kit.StopAfter(seconds=timeline[0].end + 0.1))
    assert [t.index for t in kept] == [0] and cutoff == pytest.approx(timeline[0].end + 0.1)
    kept, _ = kit.truncate(timeline, None)
    assert len(kept) == 4
    kept, _ = kit.truncate(timeline, kit.StopAfter(turns=99))
    assert len(kept) == 4


def test_pacing_defaults_and_overrides() -> None:
    assert kit.resolve_speed("kinesis", None, None) is None  # fast: no waiting
    assert kit.resolve_speed("audio", None, None) == 1.0  # live
    assert kit.resolve_speed("audio", "fast", None) == kit.AUDIO_FAST_SPEED
    assert kit.resolve_speed("kinesis", "live", None) == 1.0
    assert kit.resolve_speed("kinesis", "live", 4) == 4.0
    for bad in (0, -2, float("nan")):
        with pytest.raises(ValueError):
            kit.resolve_speed("kinesis", None, bad)
    assert kit.wall_offset(120.0, 4.0) == 30.0
    assert kit.wall_offset(120.0, None) == 0.0


# --- the records ----------------------------------------------------------------------


def _schema_input_fields(name: str) -> set[str]:
    text = SCHEMA.read_text(encoding="utf-8")
    body = re.search(rf"input {name} \{{(.*?)\n\}}", text, re.S).group(1)
    return {m.group(1) for m in re.finditer(r"^\s*(\w+)\s*:", body, re.M)}


def test_the_start_fields_are_the_ones_create_call_accepts() -> None:
    assert kit.START_FIELDS == _schema_input_fields("CreateCallInput")


def test_the_channel_names_match_the_configuration_layer() -> None:
    spec = importlib.util.spec_from_file_location("lma_channels_copy", CHANNELS_MODULE)
    channels = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(channels)
    assert (kit.REMOTE, kit.LOCAL, kit.CHAT_CHANNEL) == (
        channels.REMOTE,
        channels.LOCAL,
        channels.MEETING_CHAT,
    )


def test_the_start_record_creates_an_owned_labelled_meeting_with_its_agenda() -> None:
    meeting = kit.find_meeting("sprint_planning")
    record = kit.start_event(
        meeting, "cid", "owner-1", "2026-11-28T10:00:00+00:00", {"GuidanceEnabled": True}
    )
    assert set(record) <= kit.START_FIELDS | kit.EVENT_FIELDS
    assert record["EventType"] == "START" and record["CallId"] == "cid"
    # An empty access token is what makes the processor take AgentId as the Owner.
    assert record["AgentId"] == "owner-1" and record["AccessToken"] == ""
    assert record["Agenda"] == meeting.agenda and record["AgendaUpdatedBy"] == "owner-1"
    assert record["MeetingContext"] == meeting.meeting_context
    assert {"Key": kit.LABEL_KEY, "Value": "sprint_planning"} in record["Labels"]
    assert record["GuidanceEnabled"] is True
    assert kit.start_event(meeting, "cid", "o", "t", {"Labels": []})["Labels"] == [], (
        "extra fields win"
    )


def test_turn_and_chat_records_are_final_segments_on_their_channels() -> None:
    meeting = _meeting([{"speaker": "Bob", "text": WORDS_10}, {"chat": "Dee", "text": "typed"}])
    turn, chat = sorted(kit.build_timeline(meeting), key=lambda t: t.index)
    t = kit.segment_event("cid", turn, "now")
    c = kit.segment_event("cid", chat, "now")
    for record in (t, c):
        assert record["EventType"] == "ADD_TRANSCRIPT_SEGMENT" and record["CallId"] == "cid"
        assert record["IsPartial"] is False and record["AccessToken"] == ""
        assert set(record) == set(t)
    assert (t["Channel"], t["Speaker"], t["Transcript"]) == ("REMOTE", "Bob", WORDS_10)
    assert (t["StartTime"], t["EndTime"]) == (turn.start, turn.end)
    assert (c["Channel"], c["Speaker"], c["StartTime"]) == ("MEETING_CHAT", "Dee", c["EndTime"])
    assert t["SegmentId"] != c["SegmentId"]
    assert kit.end_event("cid", "now") == {
        "EventType": "END",
        "CallId": "cid",
        "CreatedAt": "now",
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
    }


def test_extra_start_fields_are_decoded_and_unknown_ones_named() -> None:
    extra = kit.parse_extra(
        ["GuidanceEnabled=true", "SummaryProfile=discovery-session", 'Labels=[{"Key":"a"}]']
    )
    assert extra == {
        "GuidanceEnabled": True,
        "SummaryProfile": "discovery-session",
        "Labels": [{"Key": "a"}],
    }
    assert kit.unknown_start_fields({"GuidanceEnabled": 1, "Bogus": 2}) == ["Bogus"]
    with pytest.raises(ValueError):
        kit.parse_extra(["no-equals"])


def test_the_websocket_start_asks_for_diarization_where_a_channel_has_two_voices() -> None:
    meeting = kit.find_meeting("retrospective")
    frame = kit.websocket_start_frame(meeting, "cid")
    assert "channels" not in frame, "a client 'channels' field breaks the transcriber's speaker map"
    assert frame["diarizeSystemChannel"] is True and frame["diarizeMicChannel"] is False
    assert frame["localSpeakerName"] == "Carlos Mendes" and frame["samplingRate"] == 16000
    declared = TRANSCRIBER_EVENT_TYPES.read_text(encoding="utf-8")
    for name in frame:
        assert re.search(rf"\b{name}\??:", declared), f"{name} is not a CallMetaData field"


def test_the_default_call_id_names_the_meeting_and_is_unique() -> None:
    from datetime import datetime, timezone

    meeting = kit.find_meeting("retrospective")
    when = datetime(2026, 11, 28, 13, 5, 9, tzinfo=timezone.utc)
    call_id = kit.default_call_id(meeting, when, "ab12")
    assert call_id == f"{meeting.title} - 2026-11-28 13:05:09 UTC synthetic-ab12"
    assert kit.default_call_id(meeting, when, "cd34") != call_id


def test_the_list_row_key_is_the_one_create_call_writes() -> None:
    # createCall: shard = hour / (24 / 6), PK cls#<date>#s#<2-digit shard>, SK ts#<CreatedAt>#id#<CallId>.
    assert kit.list_row_key("m", "2026-11-28T13:05:09.1+00:00") == (
        "cls#2026-11-28#s#03",
        "ts#2026-11-28T13:05:09.1+00:00#id#m",
    )
    assert kit.list_row_key("m", "2026-11-28T03:00:00Z")[0] == "cls#2026-11-28#s#00"


def test_meeting_url_is_the_ui_call_route() -> None:
    outputs = {"ApplicationCloudfrontEndpoint": "https://d1.cloudfront.net"}
    assert delivery.meeting_url(outputs, "A b/c") == "https://d1.cloudfront.net/#/calls/A%20b%2Fc"
    assert delivery.meeting_url({}, "x") is None


# --- audio ----------------------------------------------------------------------------------


def _tone(seconds: float, value: int) -> bytes:
    return value.to_bytes(2, "little", signed=True) * int(seconds * kit.SAMPLE_RATE)


def test_the_stereo_audio_puts_each_turn_on_its_channel_at_its_time() -> None:
    meeting = _meeting([{"speaker": "Bob", "text": WORDS_10}, {"speaker": "Ann", "text": WORDS_10}])
    pcm = {0: _tone(1.0, 1000), 1: _tone(2.0, -2000)}
    timeline = kit.build_timeline(meeting, {i: kit.pcm_seconds(p) for i, p in pcm.items()})
    data = kit.wav_bytes(kit.assemble_stereo(timeline, pcm))
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (2, 16000, 2)
        frames = w.readframes(w.getnframes())
        total = w.getnframes() / 16000
    remote, local = timeline[0], timeline[1]
    assert total == pytest.approx(local.end + kit.TAIL_SECONDS, abs=1e-3)

    def sample(t: float, channel: int) -> int:
        offset = (int(t * 16000) * 2 + channel) * 2
        return int.from_bytes(frames[offset : offset + 2], "little", signed=True)

    assert sample(remote.start + 0.5, 0) == 1000 and sample(remote.start + 0.5, 1) == 0
    assert sample(local.start + 0.5, 1) == -2000 and sample(local.start + 0.5, 0) == 0
    assert sample(0.5, 0) == sample(0.5, 1) == 0, "the lead-in is silent"
    assert sample(remote.end + 0.1, 0) == 0


def test_the_audio_stops_where_the_meeting_is_stopped() -> None:
    meeting = _meeting([{"speaker": "Bob", "text": WORDS_10}])
    pcm = {0: _tone(5.0, 300)}
    timeline = kit.build_timeline(meeting, {0: 5.0})
    stereo = kit.assemble_stereo(timeline, pcm, end_at=3.0)
    assert len(stereo) == 3 * 16000 * 4
    chunks = list(kit.audio_chunks(stereo))
    assert [round(t, 1) for t, _ in chunks[:3]] == [0.0, 0.2, 0.4]
    assert all(len(c) == 3200 * 4 for _, c in chunks)  # 200 ms of 16-bit stereo


def test_a_long_turn_is_split_for_polly_without_losing_words() -> None:
    text = " ".join(["This is a sentence of moderate length."] * 120)
    pieces = kit.split_for_polly(text, 300)
    assert all(len(p) <= 300 for p in pieces)
    assert " ".join(pieces).split() == text.split()
    unbroken = "word " * 200
    assert " ".join(kit.split_for_polly(unbroken, 100)).split() == unbroken.split()


class FakePolly:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def synthesize_speech(self, **kwargs):
        self.calls.append(kwargs)
        return {"AudioStream": io.BytesIO(_tone(0.5, len(self.calls)))}


def test_polly_is_asked_for_16k_pcm_once_per_turn_and_cached(tmp_path: Path) -> None:
    polly = FakePolly()
    first = delivery.synthesize(polly, "Amy", "Hello there everyone.", tmp_path)
    again = delivery.synthesize(polly, "Amy", "Hello there everyone.", tmp_path)
    assert first == again and len(polly.calls) == 1
    assert polly.calls[0] | {"Text": None} == {
        "Text": None,
        "VoiceId": "Amy",
        "Engine": "neural",
        "OutputFormat": "pcm",
        "SampleRate": "16000",
    }
    delivery.synthesize(polly, "Brian", "Hello there everyone.", tmp_path)
    assert len(polly.calls) == 2, "the cache is keyed by voice too"


# --- Kinesis ------------------------------------------------------------------------------


class ThrottledError(Exception):
    response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}


class FakeKinesis:
    def __init__(self, fail_first: int = 0, throttle_puts: int = 0) -> None:
        self.records: list[tuple[str, dict]] = []
        self.fail_first = fail_first
        self.throttle_puts = throttle_puts

    def put_record(self, StreamName, PartitionKey, Data):  # noqa: N803 - boto3's names
        if self.throttle_puts:
            self.throttle_puts -= 1
            raise ThrottledError()
        self.records.append((PartitionKey, json.loads(Data)))

    def put_records(self, StreamName, Records):  # noqa: N803
        out = []
        for record in Records:
            if self.fail_first:
                self.fail_first -= 1
                out.append({"ErrorCode": "ProvisionedThroughputExceededException"})
            else:
                self.records.append((record["PartitionKey"], json.loads(record["Data"])))
                out.append({"SequenceNumber": "1"})
        return {"Records": out}


def test_batched_records_are_retried_until_kinesis_takes_them_all() -> None:
    fake = FakeKinesis(fail_first=3)
    sender = delivery.KinesisSender(fake, "stream", "cid", sleep=lambda _: None)
    sender.put_many([{"n": i} for i in range(250)])
    assert sorted(r["n"] for _, r in fake.records) == list(range(250))
    assert {key for key, _ in fake.records} == {"cid"}, "one partition key keeps a meeting in order"
    assert sender.sent == 250


def test_a_throttled_put_is_retried() -> None:
    fake = FakeKinesis(throttle_puts=2)
    delivery.KinesisSender(fake, "stream", "cid", sleep=lambda _: None).put({"EventType": "END"})
    assert fake.records == [("cid", {"EventType": "END"})]


# --- cleanup -------------------------------------------------------------------------------


class FakeTable:
    """DynamoDB get_item/query/delete_item over attribute-value items."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def add(self, pk: str, sk: str, **attrs) -> None:
        item = {"PK": {"S": pk}, "SK": {"S": sk}}
        for key, value in attrs.items():
            item[key] = value
        self.items[(pk, sk)] = item

    def get_item(self, TableName, Key, ConsistentRead=False):  # noqa: N803
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, **_):  # noqa: N803
        assert KeyConditionExpression == "PK = :pk"
        pk = ExpressionAttributeValues[":pk"]["S"]
        return {"Items": [i for (p, _s), i in sorted(self.items.items()) if p == pk]}

    def delete_item(self, TableName, Key):  # noqa: N803
        self.items.pop((Key["PK"]["S"], Key["SK"]["S"]), None)


class FakeS3:
    def __init__(self, keys: list[str]) -> None:
        self.keys = set(keys)

    def get_paginator(self, name):
        assert name == "list_object_versions"
        s3 = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                yield {
                    "Versions": [
                        {"Key": k, "VersionId": "v1"}
                        for k in sorted(s3.keys)
                        if k.startswith(Prefix)
                    ]
                }

        return Paginator()

    def delete_object(self, Bucket, Key, VersionId):  # noqa: N803
        self.keys.discard(Key)


def _seed_meeting(table: FakeTable, call_id: str, created: str, labelled: bool = True) -> None:
    labels = (
        {"L": [{"M": {"Key": {"S": kit.LABEL_KEY}, "Value": {"S": "retrospective"}}}]}
        if labelled
        else {"L": []}
    )
    table.add(f"c#{call_id}", f"c#{call_id}", CreatedAt={"S": created}, Labels=labels)
    pk, sk = kit.list_row_key(call_id, created)
    table.add(pk, sk, CallId={"S": call_id})
    for prefix in ("trs#", "nt#", "gd#", "mtr#", "wb#", "kf#", "att#"):
        for n in range(2):
            table.add(f"{prefix}{call_id}", f"row#{n}")


def test_cleanup_removes_every_partition_of_the_meeting_and_nothing_of_another() -> None:
    table = FakeTable()
    created = "2026-11-28T13:05:09.000+00:00"
    _seed_meeting(table, "Retro - synthetic-ab12", created)
    _seed_meeting(table, "Retro - synthetic-ab12 copy", created)  # shares the same list partition
    table.add("cfg#x", "cfg#x")
    name = kit.posix_name("Retro - synthetic-ab12")
    assert name == "Retro___synthetic_ab12"
    s3 = FakeS3(
        [
            f"lma-audio-recordings/{name}.wav",
            f"lma-audio-recordings/{name}_2026-11-28T17-21-24-123Z.wav",
            f"lma-audio-recordings/{name}_2.wav",
            f"lma-audio-recordings/{name}_copy.wav",
            f"lma-transcripts/{name}-TRANSCRIPT.txt",
            f"lma-transcripts/{name}-SUMMARY.txt.metadata.json",
            f"lma-transcripts/{name}_copy-SUMMARY.txt",
        ]
    )
    before = set(table.items)
    report = delivery.cleanup(
        table,
        "t",
        "Retro - synthetic-ab12",
        s3=s3,
        bucket="b",
        prefixes=("lma-audio-recordings/", "lma-transcripts/"),
    )
    assert report.refused is None
    removed = before - set(table.items)
    assert all(
        k[0].endswith("#Retro - synthetic-ab12") or k[1].endswith("#id#Retro - synthetic-ab12")
        for k in removed
    )
    assert len(removed) == 2 + 7 * 2, "the meeting row, its list row and two rows in each partition"
    assert report.rows["trs#"] == 2 and report.list_rows == 1
    left = {k for k in table.items}
    assert ("cfg#x", "cfg#x") in left
    assert sum(1 for k in left if "copy" in k[0] or "copy" in k[1]) == 2 + 7 * 2, (
        "the other meeting is untouched"
    )
    # "..._copy.wav" begins with this meeting's file name, but is the other meeting's.
    assert s3.keys == {
        f"lma-audio-recordings/{name}_copy.wav",
        f"lma-transcripts/{name}_copy-SUMMARY.txt",
        f"lma-audio-recordings/{name}_2.wav",
    }
    # A later session's recording (<name>_<timestamp>) and the summarizer's
    # <name>-TRANSCRIPT/-SUMMARY files are this meeting's.
    assert report.objects == 4


def test_cleanup_refuses_a_meeting_the_kit_did_not_create_unless_forced() -> None:
    table = FakeTable()
    _seed_meeting(table, "Real meeting", "2026-11-28T09:00:00Z", labelled=False)
    before = dict(table.items)
    report = delivery.cleanup(table, "t", "Real meeting")
    assert report.refused and table.items == before
    report = delivery.cleanup(table, "t", "Real meeting", force=True)
    assert report.refused is None and not table.items


def test_the_recording_named_on_the_meeting_row_is_found_in_the_stack_bucket_only() -> None:
    meeting = {
        "RecordingUrl": "https://b.s3.us-east-1.amazonaws.com/lma-audio-recordings/x.wav",
        "RecordingUrls": ["https://other.s3.us-east-1.amazonaws.com/y.wav", None],
    }
    assert delivery.recording_keys(meeting, "b") == ["lma-audio-recordings/x.wav"]


# --- the WebSocket ------------------------------------------------------------------------


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list = []

        class State:
            name = "OPEN"

        self.state = State()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, message) -> None:
        self.sent.append(message)


def test_the_stream_sends_start_then_paced_audio_then_end() -> None:
    socket = FakeSocket()
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    seen: list[float] = []
    frame = {"callEvent": "START", "callId": "cid", "samplingRate": 16000}
    chunks = [(i * 0.2, bytes([i]) * 4) for i in range(5)]
    state = asyncio.run(
        delivery.stream_meeting(
            lambda url: socket,
            "wss://x",
            frame,
            chunks,
            2.0,
            seen.append,
            clock=lambda: now[0],
            sleep=sleep,
        )
    )
    assert state == "OPEN"
    assert json.loads(socket.sent[0]) == frame
    assert socket.sent[1:6] == [c for _, c in chunks]
    assert json.loads(socket.sent[-1]) == {"callEvent": "END", "callId": "cid"}
    assert seen == [t for t, _ in chunks]
    # At 2x real time, 0.8 s of meeting audio takes 0.4 s, plus the grace after END.
    assert sum(slept) == pytest.approx(0.4 + 1.0)


# --- the command line -----------------------------------------------------------------------


def test_list_and_dry_run_need_no_aws(capsys) -> None:
    assert run_meeting.main(["--list"]) == 0
    listed = capsys.readouterr().out
    assert all(name in listed for name in BUNDLED)

    assert (
        run_meeting.main(
            ["--meeting", "retrospective", "--dry-run", "--owner", "o", "--call-id", "c"]
        )
        == 0
    )
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    records = [line["record"] for line in lines]
    meeting = kit.find_meeting("retrospective")
    assert records[0]["EventType"] == "START" and records[-1]["EventType"] == "END"
    assert len(records) == 2 + len(meeting.items)
    assert all(line["at"] == 0.0 for line in lines), "Kinesis defaults to no waiting"

    assert (
        run_meeting.main(["--meeting", "retrospective", "--dry-run", "--no-end", "--speed", "2"])
        == 0
    )
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert lines[-1]["record"]["EventType"] != "END"
    assert lines[-1]["at"] == pytest.approx(lines[-1]["record"]["EndTime"] / 2, abs=0.01)


def test_the_audio_path_refuses_no_end() -> None:
    with pytest.raises(SystemExit, match="Kinesis path only"):
        run_meeting.main(["--meeting", "retrospective", "--via", "audio", "--no-end", "--dry-run"])


RESOLVER = ROOT / "lma-ai-stack/source/lambda_functions/meeting_controls_resolver/index.py"


def test_cleanup_covers_every_partition_the_meeting_delete_removes() -> None:
    """The partitions the web UI's delete removes, read from the resolver."""
    text = RESOLVER.read_text(encoding="utf-8")
    deleted = set(re.findall(r'f"([a-z]+#)\{callid\}"', text))
    deleted |= set(re.findall(r'"([a-z]+#)" \+ callid', text))
    assert {"c#", "trs#", "mtr#", "kf#", "att#", "nt#", "gd#", "wb#"} <= deleted, deleted
    assert deleted <= set(kit.MEETING_PARTITION_PREFIXES), "a partition the kit does not clean up"


def test_cleanup_deletes_the_meeting_row_last() -> None:
    table = FakeTable()
    _seed_meeting(table, "m - synthetic-1", "2026-11-28T09:00:00Z")
    order: list[str] = []
    delete = table.delete_item

    def recording(TableName, Key):  # noqa: N803
        order.append(Key["PK"]["S"])
        delete(TableName=TableName, Key=Key)

    table.delete_item = recording
    delivery.cleanup(table, "t", "m - synthetic-1")
    assert order[0].startswith("cls#") and order[-1] == "c#m - synthetic-1"
    assert not table.items
