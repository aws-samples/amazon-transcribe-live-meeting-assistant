# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_meeting_transcript on a meeting with no transcript file yet.

The file is written by the summary job when a meeting ends. For a meeting that
is still running the tool says so and points to get_live_context; for one that
has ended it says the transcript is being prepared; for any other meeting it
answers as before.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")
os.environ.setdefault("RECORDINGS_BUCKET", "recordings")

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from tools import get_transcript  # noqa: E402


class NoSuchKey(Exception):
    pass


class EmptyBucket:
    exceptions = SimpleNamespace(NoSuchKey=NoSuchKey)

    def get_object(self, **kwargs):
        raise NoSuchKey()


class Table:
    def __init__(self, item=None, error=None):
        self.item = item
        self.error = error
        self.requests = []

    def get_item(self, **kwargs):
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"Item": self.item} if self.item is not None else {}


def run(table):
    resource = SimpleNamespace(Table=lambda name: table)
    with (
        patch("boto3.client", return_value=EmptyBucket()),
        patch("boto3.resource", return_value=resource),
    ):
        get_transcript.execute("m-1", user_id="admin", is_admin=True)


def ago(seconds):
    """An UpdatedAt this many seconds before now, as LMA writes it."""
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return moment.isoformat()


def message_for(item):
    with unittest.TestCase().assertRaises(ValueError) as raised:
        run(Table(item))
    return str(raised.exception)


class TestARunningMeeting(unittest.TestCase):
    def test_a_started_or_transcribing_meeting_points_to_get_live_context(self):
        for status in ("STARTED", "TRANSCRIBING"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError) as raised:
                    run(Table({"Status": status}))
                message = str(raised.exception)
                self.assertEqual(message, get_transcript.RUNNING_MESSAGE)
                self.assertIn("still running", message)
                self.assertIn("get_live_context", message)
                self.assertIn("live meeting context is switched on", message)

    def test_an_ended_meeting_says_the_transcript_is_being_prepared(self):
        with self.assertRaises(ValueError) as raised:
            run(Table({"Status": "ENDED", "UpdatedAt": ago(30)}))
        message = str(raised.exception)
        self.assertEqual(message, get_transcript.PREPARING_MESSAGE)
        self.assertIn("being prepared", message)
        self.assertIn("try again in a minute", message)
        self.assertIn("get_meeting_summary", message)
        self.assertNotIn("m-1", message)

    def test_an_ended_meeting_with_its_file_returns_the_transcript(self):
        class Bucket(EmptyBucket):
            def get_object(self, **kwargs):
                return {"Body": SimpleNamespace(read=lambda: b"[00:00:01] Ana: hello")}

        table = Table({"Status": "ENDED"})
        resource = SimpleNamespace(Table=lambda name: table)
        with (
            patch("boto3.client", return_value=Bucket()),
            patch("boto3.resource", return_value=resource),
        ):
            result = get_transcript.execute("m-1", user_id="admin", is_admin=True)
        self.assertEqual(result["transcript"], "[00:00:01] Ana: hello")
        self.assertEqual(table.requests, [])

    def test_any_other_meeting_keeps_the_old_message(self):
        cases = (
            Table({"Status": "ERRORED"}),
            Table({"Status": "ended"}),
            Table({"Status": None}),
            Table({}),
            Table(None),
        )
        for table in cases:
            with self.subTest(item=table.item):
                with self.assertRaises(ValueError) as raised:
                    run(table)
                self.assertEqual(str(raised.exception), "Transcript not found for meeting m-1")

    def test_a_status_that_cannot_be_read_keeps_the_old_message(self):
        with self.assertRaises(ValueError) as raised:
            run(Table(error=RuntimeError("no table")))
        self.assertEqual(str(raised.exception), "Transcript not found for meeting m-1")

    def test_only_the_status_update_time_and_deletion_mark_are_read(self):
        table = Table({"Status": "ENDED"})
        with self.assertRaises(ValueError):
            run(table)
        self.assertEqual(
            table.requests,
            [
                {
                    "Key": {"PK": "c#m-1", "SK": "c#m-1"},
                    "ProjectionExpression": "#s, UpdatedAt, TranscriptDeletedAt",
                    "ExpressionAttributeNames": {"#s": "Status"},
                }
            ],
        )


class TestAnEndedMeetingWithoutAFile(unittest.TestCase):
    """Only a meeting whose file can still come is told to try again."""

    OLD = "Transcript not found for meeting m-1"

    def test_ended_within_two_hours_is_being_prepared(self):
        for age in (0, 3600, 7100):
            with self.subTest(age=age):
                message = message_for({"Status": "ENDED", "UpdatedAt": ago(age)})
                self.assertEqual(message, get_transcript.PREPARING_MESSAGE)

    def test_every_form_of_update_time_lma_writes_is_read(self):
        moment = datetime.now(timezone.utc) - timedelta(minutes=5)
        for text in (
            moment.isoformat(),
            moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            moment.replace(tzinfo=None).isoformat(),
            moment.astimezone(timezone(timedelta(hours=4))).isoformat(),
        ):
            with self.subTest(text=text):
                message = message_for({"Status": "ENDED", "UpdatedAt": text})
                self.assertEqual(message, get_transcript.PREPARING_MESSAGE)

    def test_a_deleted_transcript_keeps_the_old_message(self):
        item = {"Status": "ENDED", "UpdatedAt": ago(30), "TranscriptDeletedAt": ago(10)}

        self.assertEqual(message_for(item), self.OLD)

    def test_a_meeting_ended_more_than_two_hours_ago_keeps_the_old_message(self):
        for age in (7300, 86_400, 86_400 * 90):
            with self.subTest(age=age):
                self.assertEqual(message_for({"Status": "ENDED", "UpdatedAt": ago(age)}), self.OLD)

    def test_an_update_time_far_ahead_keeps_the_old_message(self):
        self.assertEqual(message_for({"Status": "ENDED", "UpdatedAt": ago(-3600)}), self.OLD)

    def test_an_update_time_that_cannot_be_read_keeps_the_old_message(self):
        for updated in (None, "", "yesterday", 1_700_000_000, "9999-99-99"):
            with self.subTest(updated=updated):
                item = {"Status": "ENDED"}
                if updated is not None:
                    item["UpdatedAt"] = updated
                self.assertEqual(message_for(item), self.OLD)

    def test_the_rule_on_its_own(self):
        now = 1_800_000_000.0
        stamp = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
        coming = get_transcript.transcript_may_be_coming
        self.assertTrue(coming({"Status": "ENDED", "UpdatedAt": stamp}, now))
        self.assertFalse(coming({"Status": "STARTED", "UpdatedAt": stamp}, now))
        self.assertFalse(
            coming({"Status": "ENDED", "UpdatedAt": stamp, "TranscriptDeletedAt": stamp}, now)
        )
        self.assertFalse(coming({"Status": "ENDED", "UpdatedAt": stamp}, now + 7200))
        self.assertTrue(coming({"Status": "ENDED", "UpdatedAt": stamp}, now + 7140))


if __name__ == "__main__":
    unittest.main()
