#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The MCP server library: export without credentials, import by name, round trip.

What is asserted, most consequential first:

* **No credential ever appears in an export**, for every authentication type the
  servers table holds, whether its AuthConfig is stored as JSON text or as a map,
  and whatever else the record holds. Each credential is written as an empty
  placeholder and listed in ``credentialsToReenter``.
* **An import stores no credential**, even one a hand-edited file fills in, and
  marks every server that needs credentials ``CredentialsRequired``.
* **A round trip preserves everything else**: exporting the servers an import
  created gives the original export back, credentials still empty.
* Who may call the operations: the Admin group, and IAM-signed callers (the SDK
  and CLI); not another signed-in user.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

os.environ.setdefault("MCP_SERVERS_TABLE", "mcp-servers")
os.environ.setdefault("AWS_ACCOUNT_ID", "111122223333")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, str(Path(__file__).parent))

with mock.patch("boto3.resource"), mock.patch("boto3.client"):
    import index  # noqa: E402
import mcp_library  # noqa: E402

ACCOUNT = "111122223333"

#: Every credential the records below hold. None may appear in an export.
SECRETS = [
    "sk-bearer-secret-0001",
    "header-secret-0002",
    "env-secret-0003",
    "client-secret-0004",
    "encrypted-access-0005",
    "encrypted-refresh-0006",
    "unknown-secret-0007",
    "custom-env-secret-0008",
]


def _record(server_id: str, name: str, auth: Any = None, **extra: Any) -> Dict[str, Any]:
    record = {
        "AccountId": ACCOUNT,
        "ServerId": server_id,
        "Name": name,
        "NpmPackage": extra.pop("NpmPackage", "https://mcp.example.com/" + server_id),
        "PackageType": extra.pop("PackageType", "streamable-http"),
        "Version": "latest",
        "Transport": ["streamable-http"],
        "RequiresAuth": auth is not None,
        "Status": "ACTIVE",
        "InstalledAt": "2026-01-01T00:00:00Z",
        "UpdatedAt": "2026-01-01T00:00:00Z",
        "BuildId": "build-1",
        **extra,
    }
    if record["PackageType"] == "streamable-http":
        record["ServerUrl"] = record["NpmPackage"]
    if auth is not None:
        record["AuthConfig"] = auth
    return record


#: One server per authentication type, the AuthConfig as each writer stores it.
RECORDS = [
    _record("none-server", "No auth"),
    _record("bearer-server", "Bearer", {"authType": "bearer", "token": SECRETS[0]}),
    _record(
        "headers-server",
        "Headers",
        json.dumps(
            {
                "authType": "custom_headers",
                "headers": {"X-API-Key": SECRETS[1], "X-Team": SECRETS[1]},
            }
        ),
        Description="Team tools",
    ),
    _record(
        "env-server",
        "Env vars",
        {"authType": "env_vars", "env": {"API_KEY": SECRETS[2]}},
        NpmPackage="mcp-server-example==1.2.0",
        PackageType="pypi",
    ),
    _record(
        "pypi-headers-server",
        "Pypi headers",
        {"authType": "custom_headers", "env": {"SERVICE_TOKEN": SECRETS[7]}},
        NpmPackage="@scope/server",
        PackageType="npm",
    ),
    _record(
        "client-credentials-server",
        "Client credentials",
        {
            "authType": "oauth_client_credentials",
            "oauth": {
                "grantType": "client_credentials",
                "clientId": "client-id-123",
                "clientSecret": SECRETS[3],
                "tokenUrl": "https://auth.example.com/token",
                "scopes": ["read", "write"],
                "accessToken": SECRETS[4],
            },
        },
    ),
    _record(
        "oauth2-server",
        "OAuth",
        json.dumps(
            {
                "authType": "oauth2",
                "oauth": {
                    "provider": "salesforce",
                    "clientId": "client-id-456",
                    "tokenUrl": "https://login.example.com/token",
                    "accessToken": SECRETS[4],
                    "refreshToken": SECRETS[5],
                    "expiresAt": 1234,
                    "tokenType": "Bearer",
                    "clientSecret": SECRETS[3],
                },
            }
        ),
    ),
    _record("legacy-server", "Legacy bearer", {"token": SECRETS[0]}),
    _record(
        "odd-server",
        "Odd",
        {"authType": "magic", "password": SECRETS[6], "headers": {"X": SECRETS[6]}},
    ),
]


def _export(records=RECORDS) -> Dict[str, Any]:
    return mcp_library.export_library(copy.deepcopy(records))


# ── export: no credentials ─────────────────────────────────────────────────


def test_no_credential_appears_in_an_export_for_any_authentication_type():
    text = json.dumps(_export())
    for secret in SECRETS:
        assert secret not in text
    for field in (
        "AuthConfig",
        "AccountId",
        "BuildId",
        "Status",
        "expiresAt",
        "tokenType",
        "password",
    ):
        assert field not in text


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["ServerId"])
def test_each_credential_is_an_empty_placeholder_listed_to_be_reentered(record):
    entry = mcp_library.export_server(copy.deepcopy(record))
    for path in entry["credentialsToReenter"]:
        value: Any = entry
        for part in path.split("."):
            value = value[part]
        if path == "auth":  # an unknown type: the whole of it is entered again
            assert value == {"authType": "unknown"}
            continue
        assert value in ("", {}) or (isinstance(value, dict) and set(value.values()) <= {""}), path
    assert set(_leaf_strings(entry["auth"])) <= _non_secret_strings()


def _leaf_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaf_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaf_strings(item)
    elif isinstance(value, str):
        yield value


def _non_secret_strings():
    return {
        "",
        "none",
        "bearer",
        "custom_headers",
        "env_vars",
        "oauth2",
        "oauth_client_credentials",
        "unknown",
        "client_credentials",
        "client-id-123",
        "client-id-456",
        "salesforce",
        "https://auth.example.com/token",
        "https://login.example.com/token",
        "read",
        "write",
    }


def test_the_export_keeps_the_names_of_what_must_be_entered():
    by_name = {entry["name"]: entry for entry in _export()["servers"]}
    assert by_name["Headers"]["auth"] == {
        "authType": "custom_headers",
        "headers": {"X-API-Key": "", "X-Team": ""},
    }
    assert by_name["Env vars"]["auth"]["env"] == {"API_KEY": ""}
    assert by_name["Pypi headers"]["auth"]["headers"] == {"SERVICE_TOKEN": ""}
    assert by_name["Client credentials"]["auth"]["oauth"] == {
        "grantType": "client_credentials",
        "clientId": "client-id-123",
        "tokenUrl": "https://auth.example.com/token",
        "scopes": ["read", "write"],
        "clientSecret": "",
    }
    assert by_name["OAuth"]["auth"]["oauth"]["accessToken"] == ""
    assert by_name["OAuth"]["credentialsToReenter"] == [
        "auth.oauth.accessToken",
        "auth.oauth.refreshToken",
    ]
    assert by_name["Legacy bearer"]["auth"] == {"authType": "bearer", "token": ""}
    assert by_name["Odd"]["auth"] == {"authType": "unknown"}
    assert by_name["No auth"]["credentialsToReenter"] == []


def test_the_document_names_its_kind_and_version():
    document = _export()
    assert document["kind"] == "lma-mcp-server-library"
    assert document["version"] == 1
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", document["exportedAt"])


# ── a table, for the handler ───────────────────────────────────────────────


class FakeTable:
    """Enough of a DynamoDB table for the handler's calls."""

    def __init__(self, items: List[Dict[str, Any]]):
        self.items = {item["ServerId"]: copy.deepcopy(item) for item in items}

    def query(self, **_kwargs):
        return {"Items": copy.deepcopy(list(self.items.values()))}

    def get_item(self, Key):
        item = self.items.get(Key["ServerId"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        if ConditionExpression and Item["ServerId"] in self.items:
            raise RuntimeError("ConditionalCheckFailed")
        self.items[Item["ServerId"]] = copy.deepcopy(Item)

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ExpressionAttributeNames=None,
        ConditionExpression=None,
    ):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        item = self.items.setdefault(Key["ServerId"], dict(Key))
        set_part, _, remove_part = UpdateExpression.partition(" REMOVE ")
        if remove_part == "" and UpdateExpression.startswith("REMOVE "):
            set_part, remove_part = "", UpdateExpression[len("REMOVE ") :]
        for assignment in set_part.removeprefix("SET ").split(","):
            if not assignment.strip():
                continue
            target, value = (part.strip() for part in assignment.split("="))
            item[names.get(target, target)] = copy.deepcopy(values[value])
        for target in remove_part.split(","):
            if target.strip():
                item.pop(names.get(target.strip(), target.strip()), None)


def _run(table: FakeTable, field: str, identity: Dict[str, Any], **arguments: Any):
    event = {"info": {"fieldName": field}, "identity": identity, "arguments": arguments}
    with (
        mock.patch.object(index.dynamodb, "Table", return_value=table),
        mock.patch.object(index, "codebuild") as codebuild,
    ):
        codebuild.start_build.return_value = {"build": {"id": "build-x"}}
        return index.handler(event, None)


ADMIN = {"claims": {"cognito:groups": ["Admin"]}}
USER = {"claims": {"cognito:groups": ["User"]}}
# What AppSync puts in the identity of an IAM-signed request: the signing
# principal (any non-empty value here) and no user-pool claims.
IAM = {"userArn": "the-signing-principal", "accountId": ACCOUNT}

# Five servers: the per-account limit.
FIVE = RECORDS[:5]


# ── who may call ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["exportMCPServerLibrary", "importMCPServerLibrary"])
def test_a_signed_in_user_outside_the_admin_group_cannot_use_the_library(field):
    with pytest.raises(Exception, match="Unauthorized"):
        _run(FakeTable([]), field, USER, library="{}")


@pytest.mark.parametrize("identity", [ADMIN, IAM], ids=["admin", "iam"])
def test_the_admin_group_and_an_iam_caller_can_export(identity):
    document = _run(FakeTable(RECORDS), "exportMCPServerLibrary", identity)
    assert len(document["servers"]) == len(RECORDS)


def test_entering_credentials_is_for_the_admin_group_only():
    with pytest.raises(Exception, match="Unauthorized"):
        _run(
            FakeTable([]),
            "updateMCPServerCredentials",
            IAM,
            input={"ServerId": "a", "AuthConfig": "{}"},
        )


# ── import ─────────────────────────────────────────────────────────────────


def test_an_import_stores_no_credential_even_one_the_file_fills_in():
    document = _export(FIVE)
    for entry in document["servers"]:
        auth = entry["auth"]
        if "token" in auth:
            auth["token"] = SECRETS[0]
        for key in ("headers", "env"):
            if key in auth:
                auth[key] = {name: SECRETS[1] for name in auth[key]}
    target = FakeTable([])
    result = _run(target, "importMCPServerLibrary", ADMIN, library=json.dumps(document))
    assert result["Success"] is True
    stored = json.dumps(target.items)
    for secret in SECRETS:
        assert secret not in stored
    assert all("AuthConfig" not in item for item in target.items.values())


def test_an_imported_server_that_needs_credentials_is_marked_until_they_are_entered():
    target = FakeTable([])
    result = _run(target, "importMCPServerLibrary", ADMIN, library=json.dumps(_export(FIVE)))
    flagged = {r["Name"]: r["CredentialsRequired"] for r in result["Servers"]}
    assert flagged == {
        "No auth": False,
        "Bearer": True,
        "Headers": True,
        "Env vars": True,
        "Pypi headers": True,
    }
    assert target.items["bearer-server"]["CredentialsRequired"] is True
    _run(
        target,
        "updateMCPServerCredentials",
        ADMIN,
        input={
            "ServerId": "bearer-server",
            "AuthConfig": json.dumps({"authType": "bearer", "token": "new"}),
        },
    )
    entered = target.items["bearer-server"]
    assert "CredentialsRequired" not in entered and "ImportedAuth" not in entered
    assert json.loads(entered["AuthConfig"])["token"] == "new"


def test_a_round_trip_preserves_everything_but_the_credentials():
    original = _export(FIVE)
    target = FakeTable([])
    _run(target, "importMCPServerLibrary", IAM, library=json.dumps(original))
    again = _run(target, "exportMCPServerLibrary", IAM)
    original.pop("exportedAt")
    again.pop("exportedAt")
    assert again == original


def test_every_authentication_type_survives_a_round_trip():
    # Above the five-server limit, so in two imports of up to five.
    original = _export()
    target = FakeTable([])
    for chunk in (original["servers"][:5], original["servers"][5:]):
        _run(
            target,
            "importMCPServerLibrary",
            ADMIN,
            library=json.dumps({**original, "servers": chunk}),
        )
        again = _run(target, "exportMCPServerLibrary", ADMIN)
        assert {e["name"]: e for e in again["servers"]} == {e["name"]: e for e in chunk}
        target.items.clear()


def test_an_import_updates_a_server_of_the_same_name_and_keeps_its_matching_credentials():
    installed = _record("local-bearer", "Bearer", {"authType": "bearer", "token": "kept-token"})
    target = FakeTable([installed])
    document = _export([RECORDS[1]])
    document["servers"][0]["serverUrl"] = installed["ServerUrl"]
    document["servers"][0]["description"] = "Now described"
    result = _run(target, "importMCPServerLibrary", ADMIN, library=json.dumps(document))
    assert result["Servers"][0]["Action"] == "updated"
    assert result["Servers"][0]["ServerId"] == "local-bearer"
    updated = target.items["local-bearer"]
    assert updated["Description"] == "Now described"
    assert updated["AuthConfig"]["token"] == "kept-token"
    assert "CredentialsRequired" not in updated


@pytest.mark.parametrize("auth", [{"authType": "bearer", "token": ""}, {"authType": "none"}])
def test_an_update_to_another_endpoint_drops_the_stored_credentials(auth):
    installed = _record("local-bearer", "Bearer", {"authType": "bearer", "token": "kept-token"})
    target = FakeTable([installed])
    document = _export([RECORDS[1]])
    document["servers"][0]["serverUrl"] = "https://mcp.example.com/moved"
    document["servers"][0]["auth"] = auth
    result = _run(target, "importMCPServerLibrary", ADMIN, library=json.dumps(document))
    updated = target.items["local-bearer"]
    assert updated["ServerUrl"] == "https://mcp.example.com/moved"
    assert "AuthConfig" not in updated
    needs = auth["authType"] != "none"
    assert result["Servers"][0]["CredentialsRequired"] is needs
    assert bool(updated.get("CredentialsRequired")) is needs


def test_a_package_server_moved_to_another_package_drops_its_credentials():
    target = FakeTable([RECORDS[3]])
    document = _export([RECORDS[3]])
    document["servers"][0]["package"] = "another-mcp-server==2.0"
    _run(target, "importMCPServerLibrary", ADMIN, library=json.dumps(document))
    updated = target.items["env-server"]
    assert "AuthConfig" not in updated and updated["CredentialsRequired"] is True


@pytest.mark.parametrize(
    "auth_config, message",
    [
        (json.dumps("a string"), "known authType"),
        (json.dumps({"authType": "none"}), "known authType"),
        (json.dumps({"authType": "custom_headers", "headers": {"X": "v"}}), "needs bearer"),
    ],
)
def test_entered_credentials_must_be_an_object_of_the_type_the_server_needs(auth_config, message):
    waiting = _record("waiting", "Waiting", CredentialsRequired=True, RequiredAuthType="bearer")
    target = FakeTable([waiting])
    result = _run(
        target,
        "updateMCPServerCredentials",
        ADMIN,
        input={"ServerId": "waiting", "AuthConfig": auth_config},
    )
    assert result["Success"] is False and message in result["Message"]
    assert target.items["waiting"]["CredentialsRequired"] is True


def test_an_update_to_another_authentication_type_drops_the_stored_credentials():
    installed = _record("local", "Headers", {"authType": "bearer", "token": "old-token"})
    target = FakeTable([installed])
    _run(target, "importMCPServerLibrary", ADMIN, library=json.dumps(_export([RECORDS[2]])))
    updated = target.items["local"]
    assert "AuthConfig" not in updated
    assert updated["CredentialsRequired"] is True
    assert updated["RequiredAuthType"] == "custom_headers"


def test_an_import_beyond_the_server_limit_skips_the_rest_and_says_why():
    target = FakeTable(FIVE)
    result = _run(
        target, "importMCPServerLibrary", ADMIN, library=json.dumps(_export(RECORDS[5:6]))
    )
    assert result["Servers"][0]["Action"] == "skipped"
    assert "at most 5" in result["Servers"][0]["Message"]


def test_an_entry_with_an_unacceptable_package_is_skipped_and_the_rest_imported():
    document = _export(RECORDS[:2])
    document["servers"][0]["serverUrl"] = "not a url"
    result = _run(FakeTable([]), "importMCPServerLibrary", ADMIN, library=json.dumps(document))
    assert [r["Action"] for r in result["Servers"]] == ["skipped", "created"]


@pytest.mark.parametrize(
    "library, message",
    [
        ("not json", "not valid JSON"),
        (
            json.dumps({"kind": "lma-skill-library", "version": 1, "servers": []}),
            "not an MCP server library",
        ),
        (
            json.dumps({"kind": "lma-mcp-server-library", "version": 2, "servers": []}),
            "format version",
        ),
        (json.dumps({"kind": "lma-mcp-server-library", "version": 1}), "no list of servers"),
    ],
)
def test_a_file_that_is_not_a_library_is_refused_and_nothing_is_stored(library, message):
    target = FakeTable([])
    result = _run(target, "importMCPServerLibrary", ADMIN, library=library)
    assert result["Success"] is False and message in result["Message"]
    assert target.items == {}


def test_a_package_server_import_starts_its_layer_build(monkeypatch):
    monkeypatch.setattr(index, "CODEBUILD_PROJECT", "layer-build")
    target = FakeTable([])
    result = _run(
        target, "importMCPServerLibrary", ADMIN, library=json.dumps(_export([RECORDS[3]]))
    )
    assert result["Servers"][0]["Message"] == "Layer build started (build-x)"
    assert target.items["env-server"]["Status"] == "INSTALLING"
    assert target.items["env-server"]["BuildId"] == "build-x"


def test_the_server_id_patterns_are_the_oauth_managers():
    oauth = (Path(__file__).parent.parent / "oauth_manager" / "index.py").read_text(
        encoding="utf-8"
    )
    for name in ("SERVER_ID_PATTERN", "HTTP_ENDPOINT_PATTERN"):
        pattern = getattr(mcp_library, name).pattern
        assert f'{name} = re.compile(r"{pattern}")' in oauth, name


@pytest.mark.parametrize("server_id", ["has space", "a/b/c", "-leading", "ftp://x"])
def test_an_entry_whose_server_id_is_not_an_accepted_form_is_skipped(server_id):
    document = _export([RECORDS[0]])
    document["servers"][0]["serverId"] = server_id
    result = _run(FakeTable([]), "importMCPServerLibrary", ADMIN, library=json.dumps(document))
    assert result["Servers"][0]["Action"] == "skipped"
    assert "server id" in result["Servers"][0]["Message"]


def test_reimporting_an_unchanged_package_server_starts_no_build(monkeypatch):
    monkeypatch.setattr(index, "CODEBUILD_PROJECT", "layer-build")
    target = FakeTable([RECORDS[3]])
    result = _run(
        target, "importMCPServerLibrary", ADMIN, library=json.dumps(_export([RECORDS[3]]))
    )
    assert result["Servers"][0]["Action"] == "updated"
    assert result["Servers"][0]["Message"] == ""
    assert target.items["env-server"]["Status"] == "ACTIVE"


def test_an_imported_server_lists_the_settings_its_form_opens_with_and_no_credential():
    target = FakeTable([])
    library = json.dumps(_export([RECORDS[2], RECORDS[5]]))
    _run(target, "importMCPServerLibrary", ADMIN, library=library)
    listed = {s["Name"]: s for s in _run(target, "listInstalledMCPServers", ADMIN)}
    headers = listed["Headers"]["ImportedAuthSettings"]
    assert headers == {"authType": "custom_headers", "headers": {"X-API-Key": "", "X-Team": ""}}
    oauth = listed["Client credentials"]["ImportedAuthSettings"]["oauth"]
    assert oauth["clientId"] == "client-id-123" and oauth["clientSecret"] == ""
    assert all("ImportedAuth" not in s and "AuthConfig" not in s for s in listed.values())


def test_a_server_with_its_credentials_lists_no_imported_settings():
    listed = _run(FakeTable([RECORDS[1]]), "listInstalledMCPServers", ADMIN)
    assert "ImportedAuthSettings" not in listed[0]


def test_an_entry_without_authentication_keeps_a_servers_credentials_and_clears_the_wait():
    installed = _record("local", "Bearer", {"authType": "bearer", "token": "kept-token"})
    waiting = _record("waiting", "Waiting", CredentialsRequired=True, RequiredAuthType="bearer")
    target = FakeTable([installed, waiting])
    # The same endpoints, now without authentication.
    document = _export([_record("local", "Bearer"), _record("waiting", "Waiting")])
    result = _run(target, "importMCPServerLibrary", ADMIN, library=json.dumps(document))
    assert [r["CredentialsRequired"] for r in result["Servers"]] == [False, False]
    assert target.items["local"]["AuthConfig"]["token"] == "kept-token"
    assert target.items["local"]["RequiresAuth"] is True
    assert "CredentialsRequired" not in target.items["waiting"]
