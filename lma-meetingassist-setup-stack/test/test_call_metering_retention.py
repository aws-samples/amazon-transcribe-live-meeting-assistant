#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant's usage records expire with the meeting retention.

That retention is the Retention section of the Configuration page, at most the
MeetingRecordExpirationInDays stack parameter, which the function carries as
METERING_EXPIRATION_DAYS. The values asserted are the expiry itself, not just
that one is set.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import call_metering
import pytest
from lma_config import parameters

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


class _RetentionStore:
    def __init__(self, values):
        self.values = values

    def get_section(self, name):
        return SimpleNamespace(values=self.values if name == "retention" else {})


def _expiry_days(item) -> float:
    return (int(item["ExpiresAfter"]["N"]) - NOW.timestamp()) / 86400


def _item():
    return call_metering.build_item(
        "call-1", "MeetingAssistant", "bedrock/model", {"requests": 1}, source="t", now=NOW
    )


@pytest.fixture(autouse=True)
def _no_store(monkeypatch):
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    monkeypatch.setattr(parameters, "_SHARED_STORE", None)


def test_the_stack_parameter_alone_sets_the_expiry(monkeypatch):
    monkeypatch.setenv(call_metering.EXPIRATION_ENV_VAR, "90")
    assert round(_expiry_days(_item()), 3) == 90


def test_a_shorter_retention_saved_on_the_page_is_used(monkeypatch):
    monkeypatch.setenv(call_metering.EXPIRATION_ENV_VAR, "90")
    monkeypatch.setattr(
        parameters, "_SHARED_STORE", _RetentionStore({"meetingRecordExpirationInDays": 0.5})
    )
    assert round(_expiry_days(_item()), 3) == 0.5


def test_a_retention_longer_than_the_stack_parameter_is_not_used(monkeypatch):
    monkeypatch.setenv(call_metering.EXPIRATION_ENV_VAR, "7")
    monkeypatch.setattr(
        parameters, "_SHARED_STORE", _RetentionStore({"meetingRecordExpirationInDays": 30})
    )
    assert round(_expiry_days(_item()), 3) == 7


def test_a_value_that_is_not_a_number_gives_no_expiry(monkeypatch):
    monkeypatch.setenv(call_metering.EXPIRATION_ENV_VAR, "ninety")
    assert "ExpiresAfter" not in _item()
