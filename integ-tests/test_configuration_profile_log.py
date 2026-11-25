# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Activating and deleting a configuration profile name the Admin who did it, on a deployed stack.

See docs/configuration-store.md, "Who activated or deleted a profile".

A throwaway Admin (no invitation email, a permanent password) signs in through
SRP and, through the GraphQL API as the Configuration page calls it, creates a
copy of the active profile, activates the copy, activates the profile it
replaced again and deletes the copy. ``listConfigurationProfiles`` must then
report the copy's activation on the active-profile pointer and both actions in
the profile log, each naming the Admin's email address. The copy holds the same
configuration as the profile it replaces, so meetings running meanwhile are
unaffected. The original profile is reactivated, the copy deleted and the Admin
removed in teardown, whatever the outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import role_access_probe as probe

pytest.importorskip("pycognito")

PROFILE_FIELDS = "Profiles { Name Active ActivatedBy ActivatedAt } Events { Action Profile By At Previous }"
LIST = f"query {{ listConfigurationProfiles {{ {PROFILE_FIELDS} }} }}"
CREATE = (
    "mutation ($input: CreateConfigurationProfileInput!) "
    "{ createConfigurationProfile(input: $input) { Success Profile } }"
)
ACTIVATE = (
    "mutation ($input: ConfigurationProfileNameInput!) "
    "{ activateConfigurationProfile(input: $input) { Success Profile } }"
)
DELETE = (
    "mutation ($input: ConfigurationProfileNameInput!) "
    "{ deleteConfigurationProfile(input: $input) { Success Profile } }"
)


def _data(response: dict, field: str) -> dict:
    assert not response.get("errors"), response.get("errors")
    return response["data"][field]


@pytest.fixture(scope="module")
def deployment(client) -> probe.Deployment:
    return probe.Deployment.find(client.stack_name, client.region)


@pytest.fixture(scope="module")
def admin(deployment: probe.Deployment):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    made = probe.create_role_users(deployment, stamp, accounts=(("profile-log", "Admin"),))
    try:
        probe.sign_in(deployment, made[0])
        yield made[0]
    finally:
        left = probe.delete_role_users(deployment, made)
        assert not left, f"could not delete the temporary users {left}"


def test_activating_and_deleting_a_profile_name_the_admin(deployment, admin) -> None:
    def call(document: str, field: str, **variables) -> dict:
        return _data(probe.call(deployment, admin, document, variables), field)

    listed = call(LIST, "listConfigurationProfiles")
    previous = next(p["Name"] for p in listed["Profiles"] if p["Active"])
    temporary = f"integ-profile-log-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    created = activated = False
    try:
        call(CREATE, "createConfigurationProfile", input={"Name": temporary, "CopyFrom": previous})
        created = True
        call(ACTIVATE, "activateConfigurationProfile", input={"Name": temporary})
        activated = True

        listed = call(LIST, "listConfigurationProfiles")
        (active,) = [p for p in listed["Profiles"] if p["Active"]]
        assert active["Name"] == temporary
        assert active["ActivatedBy"] == admin.username
        assert active["ActivatedAt"]
        newest = listed["Events"][0]
        assert (newest["Action"], newest["Profile"], newest["By"], newest["Previous"]) == (
            "activate",
            temporary,
            admin.username,
            previous,
        )

        call(ACTIVATE, "activateConfigurationProfile", input={"Name": previous})
        activated = False
        call(DELETE, "deleteConfigurationProfile", input={"Name": temporary})
        created = False

        events = call(LIST, "listConfigurationProfiles")["Events"]
        assert [(e["Action"], e["Profile"], e["By"]) for e in events[:2]] == [
            ("delete", temporary, admin.username),
            ("activate", previous, admin.username),
        ]
        assert all(e["At"] for e in events[:2])
    finally:
        if activated:
            probe.call(deployment, admin, ACTIVATE, {"input": {"Name": previous}})
        if created:
            probe.call(deployment, admin, DELETE, {"input": {"Name": temporary}})
