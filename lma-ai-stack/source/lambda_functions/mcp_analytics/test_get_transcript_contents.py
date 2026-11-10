# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The MCP get_meeting_transcript tool returns a past meeting's whole document.

The document is the one the summary Lambda writes for the meeting-history
knowledge base (bedrock_summary_lambda.write_to_s3): the transcript with the
meeting chat as "[chat]" lines, followed by the text of the shared screen.
S3 is a stand-in; access control is not under test here.
"""

import importlib.util
import io
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("RECORDINGS_BUCKET", "recordings")

HERE = Path(__file__).resolve().parent


def _load():
    # Loaded from its file under its own name: test_jsonrpc replaces the
    # ``tools`` package in sys.modules, so an ordinary import could get that.
    helper = types.ModuleType("tools.url_helper")
    helper.get_meeting_url = lambda meeting_id: f"https://example.com/#/calls/{meeting_id}"
    sys.modules.setdefault("tools", types.ModuleType("tools"))
    sys.modules["tools.url_helper"] = helper
    spec = importlib.util.spec_from_file_location(
        "mcp_get_transcript_under_test", HERE / "tools" / "get_transcript.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


get_transcript = _load()

DOCUMENT = (
    "\nBob: we ship on Friday"
    "\n[chat] Alice: the release notes are at https://example.com/notes"
    "\n\nSHARED SCREEN (the text of each slide, page or view shown, with the time it went up):\n"
    "[00:30] Release plan\n- Friday"
)


def _s3_holding(key: str, body: str) -> MagicMock:
    s3 = MagicMock()

    class NoSuchKey(Exception):
        pass

    s3.exceptions.NoSuchKey = NoSuchKey

    def get_object(Bucket, Key):  # noqa: N803 - boto3's names
        if Key != key:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(body.encode("utf-8"))}

    s3.get_object.side_effect = get_object
    return s3


def test_a_past_meetings_transcript_includes_its_chat_and_shared_screen_text():
    s3 = _s3_holding("lma-transcripts/Weekly_sync-TRANSCRIPT.txt", DOCUMENT)
    with (
        patch.object(get_transcript.boto3, "client", return_value=s3),
        patch.object(get_transcript, "get_meeting_url", return_value="https://example.com/m"),
    ):
        result = get_transcript.execute("Weekly sync", format="text", is_admin=True)
    assert result["transcript"] == DOCUMENT
    assert (
        "[chat] Alice: the release notes are at https://example.com/notes" in result["transcript"]
    )
    assert "[00:30] Release plan" in result["transcript"]
