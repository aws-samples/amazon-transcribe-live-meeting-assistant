# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the key frame text extractor.

Asserted here: a frame's text is read once (a frame that already has it, or
that no longer exists, costs no model call); only an image in the meeting's own
folder of this deployment's bucket is read; what the model answers is stored
trimmed, with "no text" as empty, with what the frame shows (ContentKind) and a
title, and with a looping answer kept once; every model call is metered
against the meeting; and a failure ends the invocation without raising.

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["EVENT_SOURCING_TABLE"] = "calls"
os.environ["RECORDINGS_BUCKET"] = "rec-bucket"

HERE = Path(__file__).resolve().parent


def _load():
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
    spec = importlib.util.spec_from_file_location(
        "keyframe_text_extractor_index", HERE / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()
import pure  # noqa: E402

CALL = "Weekly sync - 2026-11-01"
FRAME = "0000030000-ab12cd"
IMAGE_URL = (
    "https://rec-bucket.s3.us-east-1.amazonaws.com/"
    "lma-video-keyframes%2FWeekly_sync___2026_11_01%2F0000030000-ab12cd.jpg"
)
EVENT = {"CallId": CALL, "FrameId": FRAME, "ImageUrl": IMAGE_URL}


class Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, _limit=None) -> bytes:
        return self.data


class FakeDynamo:
    def __init__(self, item, earlier=None):
        self.item = item
        self.updates: List[Dict[str, Any]] = []
        #: The frames before this one, newest first, as a Query returns them.
        self.earlier: List[Dict[str, Any]] = earlier or []
        self.queries: List[Dict[str, Any]] = []
        self.query_error = None

    def get_item(self, **_kwargs):
        return {"Item": self.item} if self.item is not None else {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.query_error:
            raise self.query_error
        return {"Items": self.earlier[: kwargs.get("Limit", len(self.earlier))]}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


class FakeS3:
    def __init__(self, data=b"\xff\xd8jpeg\xff\xd9", error=None):
        self.data, self.error, self.keys = data, error, []

    def get_object(self, Bucket, Key):  # noqa: N803
        self.keys.append(Key)
        if self.error:
            raise self.error
        return {"Body": Body(self.data)}


ANSWER = "CONTENT: SCREEN\nTITLE: Support agenda\nAgenda item 2: Support\n- pipeline budget"

#: The measured loop: Nova Lite reading a small terminal strip of a Desktop
#: Capture App frame, until it reached its token ceiling.
LOOP = "#projects #idp-lim #develop #idp-common #develop"


class FakeBedrock:
    def __init__(self, answer=ANSWER, content=None, stop_reason="end_turn"):
        self.answer, self.calls, self.stop_reason = answer, [], stop_reason
        self.content = [{"text": answer}] if content is None else content

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": self.content}},
            "usage": {"inputTokens": 2144, "outputTokens": 30},
            "stopReason": self.stop_reason,
        }


@pytest.fixture
def fakes(monkeypatch):
    metered: List[Any] = []

    def install(item=None, s3=None, bedrock=None, earlier=None):
        dynamo = FakeDynamo({"PK": {"S": f"kf#{CALL}"}} if item is None else item, earlier)
        s3 = s3 or FakeS3()
        bedrock = bedrock or FakeBedrock()
        monkeypatch.setattr(index, "DYNAMODB", dynamo)
        monkeypatch.setattr(index, "S3", s3)
        monkeypatch.setattr(index, "BEDROCK", bedrock)
        monkeypatch.setattr(index.METERING, "record", lambda *a, **kw: metered.append(a) or True)
        return dynamo, s3, bedrock, metered

    return install


def test_a_frames_text_is_read_stored_and_metered(fakes):
    dynamo, s3, bedrock, metered = fakes()
    assert index.handler(dict(EVENT), None)["status"] == "read"
    assert s3.keys == ["lma-video-keyframes/Weekly_sync___2026_11_01/0000030000-ab12cd.jpg"]
    sent = bedrock.calls[0]["messages"][0]["content"]
    assert sent[0]["image"]["source"]["bytes"].startswith(b"\xff\xd8")
    assert "Do not describe or name people" in sent[1]["text"]
    update = dynamo.updates[0]
    assert update["Key"] == {"PK": {"S": f"kf#{CALL}"}, "SK": {"S": f"f#{FRAME}"}}
    values = update["ExpressionAttributeValues"]
    assert values[":t"] == {"S": "Agenda item 2: Support\n- pipeline budget"}
    assert values[":k"] == {"S": "SCREEN"}
    assert values[":n"] == {"S": "Support agenda"}
    assert "ContentKind = :k" in update["UpdateExpression"]
    assert "Title = :n" in update["UpdateExpression"]
    assert bedrock.calls[0]["inferenceConfig"]["maxTokens"] == index.MAX_OUTPUT_TOKENS == 1200
    assert update["ConditionExpression"] == "attribute_exists(PK)"
    assert metered == [
        (
            CALL,
            "SlideText",
            "bedrock/amazon.nova-lite-v1:0",
            {"inputTokens": 2144, "outputTokens": 30, "requests": 1},
        )
    ]


def test_a_frame_that_already_has_its_text_costs_nothing(fakes):
    _, s3, bedrock, metered = fakes(item={"PK": {"S": f"kf#{CALL}"}, "SlideText": {"S": "x"}})
    assert index.handler(dict(EVENT), None) == {"status": "skipped", "reason": "already read"}
    assert s3.keys == [] and bedrock.calls == [] and metered == []


def test_a_frame_that_is_not_stored_costs_nothing(fakes):
    dynamo, s3, bedrock, _ = fakes(item={})
    dynamo.item = None
    assert index.handler(dict(EVENT), None)["reason"] == "no frame"
    assert bedrock.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"ImageUrl": IMAGE_URL.replace("rec-bucket", "other-bucket")},
        {"ImageUrl": IMAGE_URL.replace("Weekly_sync___2026_11_01", "Another_meeting")},
        {"ImageUrl": "s3://rec-bucket/lma-video-keyframes/Weekly_sync___2026_11_01/f.jpg"},
        {"FrameId": "f#bad"},
        {"CallId": ""},
    ],
)
def test_only_the_meetings_own_images_are_read(fakes, change):
    _, s3, bedrock, _ = fakes()
    assert index.handler({**EVENT, **change}, None)["status"] == "skipped"
    assert s3.keys == [] and bedrock.calls == []


def test_a_picture_with_no_text_is_stored_as_empty(fakes):
    dynamo, *_ = fakes(bedrock=FakeBedrock(answer="NO TEXT."))
    index.handler(dict(EVENT), None)
    assert dynamo.updates[0]["ExpressionAttributeValues"][":t"] == {"S": ""}


def test_a_camera_frame_is_stored_with_its_kind_and_no_text_or_title(fakes):
    dynamo, *_ = fakes(bedrock=FakeBedrock(answer="CONTENT: CAMERA\nTITLE: none\nAlice\nMute"))
    assert index.handler(dict(EVENT), None)["contentKind"] == "CAMERA"
    update = dynamo.updates[0]
    assert update["ExpressionAttributeValues"][":k"] == {"S": "CAMERA"}
    assert update["ExpressionAttributeValues"][":t"] == {"S": ""}
    assert ":n" not in update["ExpressionAttributeValues"]
    assert "Title" not in update["UpdateExpression"]


def test_an_answer_at_the_token_ceiling_keeps_its_text_with_the_loop_collapsed(fakes):
    answer = "CONTENT: MIXED\nTITLE: Sprint review\nSprint review\n" + " ".join([LOOP] * 60)
    dynamo, *_ = fakes(bedrock=FakeBedrock(answer=answer + "\n#proj", stop_reason="max_tokens"))
    assert index.handler(dict(EVENT), None)["status"] == "read"
    values = dynamo.updates[0]["ExpressionAttributeValues"]
    assert values[":t"] == {"S": f"Sprint review\n{LOOP}"}
    assert values[":k"] == {"S": "MIXED"}


def test_failures_end_the_invocation_without_raising(fakes):
    err = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject")
    dynamo, *_ = fakes(s3=FakeS3(error=err))
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "image unreadable"}
    assert dynamo.updates == []


def test_an_answer_with_no_text_ends_the_invocation_without_raising(fakes):
    dynamo, *_ = fakes(bedrock=FakeBedrock(content=[{"reasoningContent": {"reasoningText": {}}}]))
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "no answer"}
    assert dynamo.updates == []


def test_table_errors_end_the_invocation_without_raising(fakes):
    dynamo, *_ = fakes()
    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "x"}}, "UpdateItem"
    )

    def slow(**_kwargs):
        raise throttled

    dynamo.update_item = slow
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "not stored"}
    dynamo.get_item = slow
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "frame unreadable"}


def test_the_function_is_not_retried_after_a_failure():
    template = (HERE.parents[2] / "deployment" / "lma-ai-stack.yaml").read_text(encoding="utf-8")
    block = template.split("  KeyframeTextExtractorFunction:\n", 1)[1].split(
        "\n  KeyframeTextExtractorFunctionLogGroup:", 1
    )[0]
    assert "EventInvokeConfig:\n        MaximumRetryAttempts: 0" in block


def test_a_frame_deleted_meanwhile_is_left_deleted(fakes):
    dynamo, *_ = fakes()

    def gone(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem"
        )

    dynamo.update_item = gone
    assert index.handler(dict(EVENT), None) == {"status": "skipped", "reason": "frame deleted"}


# ── repeated frames ─────────────────────────────────────────────────────────


def earlier_frame(frame_id, text=None, title=None, kind="SCREEN", duplicate_of=None):
    """A frame before the one being read, as the Query returns it (text None: not read yet)."""
    item: Dict[str, Any] = {"FrameId": {"S": frame_id}}
    if text is not None:
        item["SlideText"] = {"S": text}
        item["ContentKind"] = {"S": kind}
    if title:
        item["Title"] = {"S": title}
    if duplicate_of:
        item["DuplicateOf"] = {"S": duplicate_of}
    return item


#: A live page as read on two frames a minute apart: the same screen, with
#: a few of its lines updated.
PAGE = "\n".join(
    [
        "Meeting details",
        "Status: in progress",
        "Duration 41:12",
        "Transcript",
        "Alice: the queue is draining faster now",
        "Bob: we should cut the batch size again",
        "Alice: agreed, let's try 500",
        "Carol: and watch the error rate",
        "Bob: it held at zero overnight",
        "Summary pending",
    ]
)
PAGE_LATER = PAGE.replace("41:12", "42:30").replace(
    "Carol: and watch the error rate", "Dan: what about the retries"
)


def answer(title, text, kind="SCREEN"):
    return f"CONTENT: {kind}\nTITLE: {title}\n{text}"


def stored_duplicate_of(dynamo):
    return dynamo.updates[0]["ExpressionAttributeValues"].get(":d", {}).get("S")


def test_a_frame_showing_the_same_screen_as_the_one_before_is_marked_a_duplicate(fakes):
    dynamo, *_ = fakes(
        bedrock=FakeBedrock(answer=answer("Meeting details page", PAGE_LATER)),
        earlier=[earlier_frame("0000020000-aaaaaa", PAGE, "Meeting details page")],
    )
    result = index.handler(dict(EVENT), None)
    assert result["duplicateOf"] == "0000020000-aaaaaa"
    assert stored_duplicate_of(dynamo) == "0000020000-aaaaaa"
    assert "DuplicateOf = :d" in dynamo.updates[0]["UpdateExpression"]
    query = dynamo.queries[0]
    assert query["KeyConditionExpression"] == "PK = :pk AND SK < :sk"
    assert query["ExpressionAttributeValues"] == {
        ":pk": {"S": f"kf#{CALL}"},
        ":sk": {"S": f"f#{FRAME}"},
    }
    assert query["ScanIndexForward"] is False


def test_text_alike_enough_is_a_duplicate_whatever_the_titles(fakes):
    dynamo, *_ = fakes(
        bedrock=FakeBedrock(answer=answer("Live transcript", PAGE_LATER)),
        earlier=[earlier_frame("0000020000-aaaaaa", PAGE, "Meeting details page")],
    )
    index.handler(dict(EVENT), None)
    assert pure.text_similarity(PAGE, PAGE_LATER) >= pure.DUPLICATE_TEXT_SIMILARITY
    assert stored_duplicate_of(dynamo) == "0000020000-aaaaaa"


def test_the_same_title_with_much_the_same_text_is_a_duplicate():
    half = "\n".join(PAGE.split("\n")[:6] + ["Eve: new topic", "Frank: budget", "Gail: hiring"])
    similarity = pure.text_similarity(PAGE, half)
    assert pure.DUPLICATE_TITLE_TEXT_SIMILARITY <= similarity < pure.DUPLICATE_TEXT_SIMILARITY
    assert pure.is_duplicate("Meeting details page", half, "meeting details page.", PAGE)
    assert not pure.is_duplicate("Agenda", half, "Meeting details page", PAGE)


def test_slides_of_one_deck_with_the_same_title_are_not_duplicates(fakes):
    # A model may name the deck rather than the slide; the slides' text differs.
    dynamo, *_ = fakes(
        bedrock=FakeBedrock(answer=answer("Q3 review", "Hiring\n- 4 offers out\n- 2 accepted")),
        earlier=[
            earlier_frame("0000020000-aaaaaa", "Revenue\n- up 12%\n- churn flat", "Q3 review")
        ],
    )
    assert "duplicateOf" not in index.handler(dict(EVENT), None)
    assert ":d" not in dynamo.updates[0]["ExpressionAttributeValues"]


def test_a_slide_built_up_a_point_at_a_time_is_not_a_duplicate():
    earlier = "Roadmap\n- Search\n- Sharing\n- Export\n- Mobile app\n- Offline mode"
    fuller = earlier + "\n- Admin console"
    assert pure.text_similarity(fuller, earlier) >= pure.DUPLICATE_TEXT_SIMILARITY
    assert not pure.is_duplicate("Roadmap", fuller, "Roadmap", earlier)
    # The same slide with a point taken away is the same screen, though.
    assert pure.is_duplicate("Roadmap", earlier, "Roadmap", fuller)


def test_a_duplicate_names_the_original_passing_over_camera_frames_and_other_duplicates(fakes):
    dynamo, *_ = fakes(
        bedrock=FakeBedrock(answer=answer("Meeting details page", PAGE_LATER)),
        earlier=[
            earlier_frame("0000040000-cccccc", "", None, kind="CAMERA"),
            earlier_frame(
                "0000030000-bbbbbb", PAGE, "Meeting details page", duplicate_of="0000020000-aaaaaa"
            ),
            earlier_frame("0000020000-aaaaaa", PAGE, "Meeting details page"),
        ],
    )
    index.handler(dict(EVENT), None)
    assert stored_duplicate_of(dynamo) == "0000020000-aaaaaa"


def test_no_frame_is_marked_while_the_one_before_is_not_read_yet(fakes):
    dynamo, *_ = fakes(
        bedrock=FakeBedrock(answer=answer("Meeting details page", PAGE)),
        earlier=[
            earlier_frame("0000030000-bbbbbb"),
            earlier_frame("0000020000-aaaaaa", PAGE, "Meeting details page"),
        ],
    )
    index.handler(dict(EVENT), None)
    assert ":d" not in dynamo.updates[0]["ExpressionAttributeValues"]


def test_the_first_frame_and_camera_frames_are_never_marked(fakes):
    dynamo, *_ = fakes(bedrock=FakeBedrock(answer=answer("Meeting details page", PAGE)))
    index.handler(dict(EVENT), None)
    assert ":d" not in dynamo.updates[0]["ExpressionAttributeValues"]

    dynamo, *_ = fakes(
        bedrock=FakeBedrock(answer="CONTENT: CAMERA\nTITLE: none\nAlice"),
        earlier=[earlier_frame("0000020000-aaaaaa", "", None, kind="CAMERA")],
    )
    index.handler(dict(EVENT), None)
    assert dynamo.queries == [] and ":d" not in dynamo.updates[0]["ExpressionAttributeValues"]


def test_a_frame_is_stored_unmarked_when_the_frames_before_it_cannot_be_read(fakes):
    dynamo, *_ = fakes(
        bedrock=FakeBedrock(answer=answer("Meeting details page", PAGE)),
        earlier=[earlier_frame("0000020000-aaaaaa", PAGE, "Meeting details page")],
    )
    dynamo.query_error = ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "Query")
    assert index.handler(dict(EVENT), None)["status"] == "read"
    assert ":d" not in dynamo.updates[0]["ExpressionAttributeValues"]


def test_frames_without_text_are_alike_only_by_their_title():
    assert not pure.is_duplicate("", "", "", "")
    assert pure.is_duplicate("Architecture diagram", "", "Architecture diagram", "")
    assert not pure.is_duplicate("Architecture diagram", "Step 1", "Architecture diagram", "")


def test_the_function_may_read_only_the_meetings_key_frames_to_compare_them():
    template = (HERE.parents[2] / "deployment" / "lma-ai-stack.yaml").read_text(encoding="utf-8")
    role = template.split("  KeyframeTextExtractorFunctionExecutionRole:\n", 1)[1]
    statement = role.split("# The key frame items", 1)[1].split("- Effect", 2)[1]
    assert "dynamodb:Query" in statement
    assert '"kf#*"' in statement


# ── pure ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "answer, stored",
    [
        ("  Title\n- one  ", "Title\n- one"),
        ("```\nTitle\n```", "Title"),
        ("NO TEXT", ""),
        ("no text.", ""),
        (None, ""),
    ],
)
def test_the_answer_is_trimmed_before_it_is_stored(answer, stored):
    assert pure.clean_text(answer) == stored


def test_a_runaway_answer_is_cut_at_a_line():
    text = pure.clean_text("".join(f"line {n} of the text\n" for n in range(2000)))
    assert len(text) <= pure.MAX_TEXT_CHARS
    assert text.endswith("of the text")


@pytest.mark.parametrize(
    "answer, read",
    [
        (ANSWER, ("SCREEN", "Support agenda", "Agenda item 2: Support\n- pipeline budget")),
        (
            '**CONTENT:** mixed\n**TITLE:** "Q3 revenue."\n\nUp 10%',
            ("MIXED", "Q3 revenue", "Up 10%"),
        ),
        ("```\nCONTENT: SCREEN\nTITLE: none\nRoadmap\n```", ("SCREEN", "", "Roadmap")),
        ("CONTENT: SCREEN\nRoadmap", ("SCREEN", "", "Roadmap")),
        ("CONTENT: SCREEN\nTITLE: Plan\nNO TEXT", ("SCREEN", "Plan", "")),
        ("CONTENT: CAMERA\nTITLE: Weekly sync\nAlice\nBob", ("CAMERA", "", "")),
        # No header, as every answer was before it was asked for: SCREEN, all text.
        ("Agenda\n- one", ("SCREEN", "", "Agenda\n- one")),
        # A slide's own first line is not taken for the header.
        ("Title: Roadmap 2027\n- hiring", ("SCREEN", "", "Title: Roadmap 2027\n- hiring")),
        ("Content: what we cover\n- hiring", ("SCREEN", "", "Content: what we cover\n- hiring")),
        ("CONTENT: SLIDES?\nTITLE: x\nBody", ("SCREEN", "", "CONTENT: SLIDES?\nTITLE: x\nBody")),
        (None, ("SCREEN", "", "")),
    ],
)
def test_the_answer_header_is_read_leniently_and_only_as_a_header(answer, read):
    assert pure.read_answer(answer) == read


def test_a_long_title_is_cut_to_a_few_words():
    _, title, _ = pure.read_answer("CONTENT: SCREEN\nTITLE: " + " ".join(["word"] * 30) + "\nx")
    assert len(title.split()) == pure.MAX_TITLE_WORDS


@pytest.mark.parametrize(
    "answer, stored",
    [
        # The measured loop, on one line and on a line each: kept once.
        (" ".join([LOOP] * 40), LOOP),
        ("\n".join([LOOP] * 40), LOOP),
        (
            "Sprint review\n- Q3 goals\n" + " ".join([LOOP] * 40),
            f"Sprint review\n- Q3 goals\n{LOOP}",
        ),
        # A cycle of a few lines.
        ("Heading\n" + "alpha\nbeta\n" * 10, "Heading\nalpha\nbeta"),
        # A slide's own short repeats and a table are left as they are.
        ("Yes\nYes\nNo", "Yes\nYes\nNo"),
        ("very very good", "very very good"),
        (
            "| a | b | c | d | e |\n| --- | --- | --- | --- | --- |",
            "| a | b | c | d | e |\n| --- | --- | --- | --- | --- |",
        ),
    ],
)
def test_a_looping_answer_is_kept_once(answer, stored):
    assert pure.clean_text(answer) == stored


def test_text_that_is_still_mostly_repetition_is_dropped():
    # A loop that varies as it goes, so no run of it repeats exactly.
    words = ["#projects", "#idp-lim", "#develop", "#idp-common", "#main"]
    pick = random.Random(7)
    answer = " ".join(pick.choice(words) for _ in range(200))
    assert pure.clean_text(answer) == ""


def test_a_page_of_templated_rows_is_not_taken_for_repetition():
    page = "\n".join(
        f"{n}. The quarterly plan item {n} covers budget, hiring and the roadmap for team {n}"
        for n in range(1, 23)
    )
    assert pure.clean_text(page) == page


def test_a_truncated_answer_loses_its_cut_off_last_line():
    assert pure.clean_text("Agenda\n- one\n- tw", truncated=True) == "Agenda\n- one"
    assert pure.clean_text("Agenda", truncated=True) == "Agenda"


@pytest.mark.parametrize(
    "line",
    [
        "def handler(event,  context):    return  None",
        "Region      Q1      Q2      Q3",
        "  indented  code   line  with  spaces",
    ],
)
def test_a_line_with_no_repeated_run_keeps_its_spacing(line):
    assert pure.collapse_repetition(line) == line
    assert pure.collapse_repetition("Heading\n" + line) == "Heading\n" + line


def test_a_line_with_a_repeated_run_is_collapsed():
    line = "#projects #idp-lim #develop " * 6 + "end"
    assert pure.collapse_repetition(line) == "#projects #idp-lim #develop end"
