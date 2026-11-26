#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Each person's own names in meetings, on the My consents page.

What a person depends on: they change only their own names, kept under their
own verified address with their account's sub; the names are within their
limits; and the user pool's name, when they keep none, is shown as the one
that applies. No AWS.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_index as base  # noqa: E402  pylint: disable=wrong-import-position

index = base.index
aws = base.aws
ANA, BO = base.ANA, base.BO


def update(username, **data):
    return base.call("updateMyAccountNames", username, {"input": data})


def test_a_person_keeps_their_names_under_their_own_address(aws) -> None:
    answer = update(ANA, DisplayName=" Ana  Silva ", ShortNames=["Ani", "ani", "Ana Silva"])
    assert answer["DisplayName"] == "Ana Silva" and answer["ShortNames"] == ["Ani"]
    item = aws["names"].items[(f"pmc#{ANA}", "account-names")]
    assert item["Subject"]["S"] == f"sub-{ANA}"
    assert item["UpdatedBy"]["S"] == ANA
    # Their search entries are written with them.
    assert ("acn#ana", f"ana silva\t{ANA}") in aws["names"].items
    mine = base.call("listMyAdvanceConsents", ANA)["Names"]
    assert (mine["DisplayName"], mine["ShortNames"]) == ("Ana Silva", ["Ani"])
    assert base.call("listMyAdvanceConsents", BO)["Names"]["DisplayName"] is None


def test_names_outside_the_limits_are_refused_and_nothing_is_kept(aws) -> None:
    for data in (
        {"DisplayName": "x" * 101},
        {"ShortNames": ["a1", "b2", "c3", "d4", "e5", "f6"]},
        {"ShortNames": ["y" * 51]},
        {"DisplayName": "Ana <ana@example.com>"},
    ):
        with pytest.raises(index.BadRequest):
            update(ANA, **data)
    assert aws["names"].items == {}


def test_the_user_pool_name_is_given_as_the_accounts_name(aws) -> None:
    aws["cognito"].users[ANA]["name"] = "Ana S."
    names = base.call("listMyAdvanceConsents", ANA)["Names"]
    assert names == {
        "DisplayName": None,
        "ShortNames": [],
        "AccountName": "Ana S.",
        "UpdatedAt": None,
    }


def test_names_kept_for_an_earlier_account_with_the_address_are_not_shown(aws) -> None:
    update(ANA, DisplayName="Ana Silva")
    aws["cognito"].users[ANA]["sub"] = "sub-of-a-new-account"
    caller = base.event("listMyAdvanceConsents", ANA)
    caller["identity"]["claims"]["sub"] = "sub-of-a-new-account"
    assert index.handler(caller, None)["Names"]["DisplayName"] is None


def test_clearing_the_names_removes_them(aws) -> None:
    update(ANA, DisplayName="Ana Silva", ShortNames=["Ani"])
    answer = update(ANA, DisplayName="", ShortNames=[])
    assert answer["DisplayName"] is None and answer["ShortNames"] == []
    assert aws["names"].items == {}


def test_a_signed_request_with_no_person_is_refused(aws) -> None:
    with pytest.raises(index.Refused):
        index.handler(
            {
                "info": {"fieldName": "updateMyAccountNames"},
                "arguments": {"input": {}},
                "identity": {},
            },
            None,
        )
