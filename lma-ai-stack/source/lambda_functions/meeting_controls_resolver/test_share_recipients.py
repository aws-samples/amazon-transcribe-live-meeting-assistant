# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Which recipients shareMeetings accepts.

Same setup as test_delete_metering_records.py: no AWS, the AppSync client and
the GraphQL DSL replaced before import.
"""

from __future__ import annotations

import pytest
from test_delete_metering_records import index


@pytest.mark.parametrize(
    "recipients",
    [
        "a@example.com",
        "a@example.com,b@example.com",
        "a@example.com, b@example.com",
        " a@example.com ",
    ],
)
def test_email_addresses_are_accepted(recipients: str) -> None:
    assert all(index.RECIPIENT_PATTERN.fullmatch(email.strip()) for email in recipients.split(","))


@pytest.mark.parametrize(
    "recipients",
    [
        "not-an-address",
        "[a@example.com",
        "a@example.com]",
        "a@example.com,[b@example.com]",
        "a]@example.com",
        "a@example.com,",
    ],
)
def test_a_recipient_that_is_not_a_plain_address_is_refused(monkeypatch, recipients: str) -> None:
    def no_appsync(*_args, **_kwargs):
        raise AssertionError("a refused share reaches AppSync")

    monkeypatch.setattr(index, "share_meeting", no_appsync)
    result = index.share_meetings([{"CallId": "c1", "ListPK": "l", "ListSK": "s"}], "o", recipients)
    assert result == {"Result": "Invalid email address provided"}


def test_sharing_reads_every_page_of_the_meeting_transcript(monkeypatch) -> None:
    """A long meeting's segments span pages, one of which can be empty."""
    from unittest import mock

    tokens = []

    class Field:
        def args(self, **kwargs):
            tokens.append(kwargs["nextToken"])
            return self

        def select(self, *_fields):
            return tokens[-1]

    schema = mock.MagicMock()
    schema.Query.getTranscriptSegments = Field()
    monkeypatch.setattr(index, "DSLQuery", lambda token: token, raising=False)
    monkeypatch.setattr(index, "dsl_gql", lambda token: token, raising=False)
    pages = {
        None: {"TranscriptSegments": [{"PK": "trs#c1", "SK": "a"}], "nextToken": "t1"},
        "t1": {"TranscriptSegments": [], "nextToken": "t2"},
        "t2": {"TranscriptSegments": [{"PK": "trs#c1", "SK": "b"}, None], "nextToken": None},
    }
    session = mock.MagicMock()
    session.execute.side_effect = lambda token: {"getTranscriptSegments": pages[token]}

    segments = index.get_transcript_segments(session, schema, "c1")

    assert [segment["SK"] for segment in segments] == ["a", "b"]
    assert tokens == [None, "t1", "t2"]
