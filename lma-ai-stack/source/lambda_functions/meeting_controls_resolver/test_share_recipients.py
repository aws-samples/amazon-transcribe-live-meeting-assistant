# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
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
        raise AssertionError("a refused share is written")

    monkeypatch.setattr(index, "record_shares", no_appsync)
    result = index.share_meetings([{"CallId": "c1", "ListPK": "l", "ListSK": "s"}], "o", recipients)
    assert result == {"Result": "Invalid email address provided"}


def test_sharing_reads_every_page_of_the_meeting_transcript() -> None:
    """A long meeting's items span pages, one of which can be empty; and a fan-out resumes."""
    queries = []

    class Table:
        pages = {
            None: {"Items": [{"PK": "trs#c1", "SK": "a"}], "LastEvaluatedKey": {"SK": "a"}},
            "a": {"Items": [], "LastEvaluatedKey": {"SK": "a2"}},
            "a2": {"Items": [{"PK": "trs#c1", "SK": "b"}]},
        }

        def query(self, **kwargs):
            queries.append(kwargs)
            return self.pages[(kwargs.get("ExclusiveStartKey") or {}).get("SK")]

    original = index.ddbTable
    index.ddbTable = Table()
    try:
        assert [key["SK"] for key in index._partition_keys("trs#c1")] == ["a", "b"]
        assert all(q["ConsistentRead"] for q in queries)
        # From a fan-out's Cursor: the items after the last one it gave the list.
        queries.clear()
        assert [key["SK"] for key in index._partition_keys("trs#c1", after="a2")] == ["b"]
        assert queries[0]["ExclusiveStartKey"] == {"PK": "trs#c1", "SK": "a2"}
    finally:
        index.ddbTable = original
