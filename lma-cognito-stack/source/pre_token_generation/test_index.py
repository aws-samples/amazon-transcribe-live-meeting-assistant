# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the pre token generation trigger (no AWS calls).

The trigger is what lets an AppSync field name the User role at all: without it
a user in no group carries no cognito:groups claim, and a field listing
["Admin", "User"] would refuse every existing User. These tests pin the role
rule to the role matrix's vectors and the response to the version 1 event shape
Cognito reads.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROLE_MATRIX = HERE.parents[2] / "lma-ai-stack" / "source" / "appsync" / "role-matrix.json"


def _load_index():
    spec = importlib.util.spec_from_file_location("pre_token_generation_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


def _event(groups, trigger="TokenGeneration_Authentication", **configuration):
    return {
        "version": "1",
        "triggerSource": trigger,
        "userName": "someone@example.com",
        "request": {
            "userAttributes": {"sub": "a1b2", "email": "someone@example.com"},
            "groupConfiguration": {"groupsToOverride": groups, **configuration},
        },
        "response": {},
    }


def _vectors() -> list[dict]:
    return json.loads(ROLE_MATRIX.read_text())["effectiveRole"]["vectors"]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: ",".join(v["groups"]) or "none")
def test_the_role_rule_matches_the_role_matrix(vector: dict) -> None:
    assert index.effective_role(vector["groups"]) == vector["role"]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: ",".join(v["groups"]) or "none")
def test_every_token_carries_exactly_one_role_group(vector: dict) -> None:
    response = index.lambda_handler(_event(list(vector["groups"])), None)["response"]
    groups = response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"]

    assert [group for group in groups if group in ("Admin", "User", "Participant")] == [
        vector["role"]
    ]
    assert response["claimsOverrideDetails"]["claimsToAddOrOverride"] == {
        "lma_role": vector["role"]
    }


def test_a_user_in_no_group_carries_the_user_group() -> None:
    response = index.lambda_handler(_event([]), None)["response"]

    assert response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"] == ["User"]


def test_an_admin_who_is_also_a_participant_carries_admin_only() -> None:
    """Otherwise the identity pool's Participant rule would match an Admin."""
    response = index.lambda_handler(_event(["Participant", "Admin"]), None)["response"]

    assert response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"] == [
        "Admin"
    ]


def test_a_participant_in_a_group_called_user_carries_participant_only() -> None:
    """A group of that name must not lift a Participant to the User role."""
    response = index.lambda_handler(_event(["User", "Participant"]), None)["response"]

    assert response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"] == [
        "Participant"
    ]


def test_groups_other_than_the_role_groups_are_kept_in_order() -> None:
    response = index.lambda_handler(_event(["Sales", "Admin", "Emea"]), None)["response"]

    assert response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"] == [
        "Sales",
        "Emea",
        "Admin",
    ]


def test_the_group_iam_roles_are_passed_through() -> None:
    """The override replaces the group configuration, IAM roles included."""
    roles = ["example-group-role"]
    event = _event(["Sales"], iamRolesToOverride=roles, preferredRole=roles[0])

    details = index.lambda_handler(event, None)["response"]["claimsOverrideDetails"]

    assert details["groupOverrideDetails"]["iamRolesToOverride"] == roles
    assert details["groupOverrideDetails"]["preferredRole"] == roles[0]


def test_no_iam_roles_are_added_when_the_groups_have_none() -> None:
    details = index.lambda_handler(_event(["Admin"]), None)["response"]["claimsOverrideDetails"]

    assert set(details["groupOverrideDetails"]) == {"groupsToOverride"}


@pytest.mark.parametrize(
    "trigger",
    [
        "TokenGeneration_HostedAuth",
        "TokenGeneration_Authentication",
        "TokenGeneration_NewPasswordChallenge",
        "TokenGeneration_AuthenticateDevice",
        "TokenGeneration_RefreshTokens",
    ],
)
def test_every_sign_in_and_refresh_flow_gets_the_role(trigger: str) -> None:
    """The browser extension uses managed login, the web UI SRP, every client refresh."""
    details = index.lambda_handler(_event(["Participant"], trigger), None)["response"][
        "claimsOverrideDetails"
    ]

    assert details["groupOverrideDetails"]["groupsToOverride"] == ["Participant"]


def test_an_event_without_a_group_configuration_is_a_user() -> None:
    event = {"version": "1", "triggerSource": "TokenGeneration_Authentication", "request": {}}

    details = index.lambda_handler(event, None)["response"]["claimsOverrideDetails"]

    assert details["groupOverrideDetails"]["groupsToOverride"] == ["User"]
    assert details["claimsToAddOrOverride"] == {"lma_role": "User"}
