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
