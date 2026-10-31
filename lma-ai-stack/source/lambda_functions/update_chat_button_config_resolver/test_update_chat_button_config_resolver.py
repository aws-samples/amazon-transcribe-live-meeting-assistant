# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the chat button resolver (no AWS calls).

This resolver serves both halves of the chat buttons from the shared
configuration table: the read every signed-in user's chat panel makes, and the
write the Edit Chat Buttons dialog makes. The behaviours asserted here are the
ones that are silent when wrong:

* **The read carries the buttons and nothing else that says who saved them.**
  Every signed-in user reads this record, and the stored profile also records
  the administrator who saved it.
* **Only "N#Label" fields are stored**, and a save replaces this dialog's
  section and nothing else, because the one profile holds every configuration
  page's settings.
* **A concurrent save is refused, not applied.**
* **The write is for the Admin group**, re-checked here and not only on the
  AppSync field; the read is not restricted, because the chat panel depends on
  it for every user.
* **The audit attributes come from the verified identity**, never from the
  request.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
# source/lambda_functions/update_chat_button_config_resolver -> source
LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "lma-configuration")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from lma_config import codec  # noqa: E402
from lma_config.keys import ACTIVE_POINTER_KEY, DEFAULT_KEY  # noqa: E402
from lma_config.store import ConfigurationStore  # noqa: E402

# Loaded by path under a unique name: each config resolver's handler module is
# called `index.py`, so a plain `import index` would resolve to whichever one a
# shared test session imported first.
_SPEC = importlib.util.spec_from_file_location(
    "update_chat_button_config_resolver_index", HERE / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

ADMIN_IDENTITY = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}
OTHER_ADMIN = {"claims": {"cognito:groups": ["Admin"], "email": "someone-else@example.com"}}
USER_IDENTITY = {"claims": {"cognito:groups": ["Users"], "email": "user@example.com"}}
IAM_IDENTITY = {
    "accountId": "123456789012",
    "userArn": "arn:aws:sts::123456789012:assumed-role/a-service-role/session",
}

SECTION = "chat_buttons"
CUSTOM_KEY = "Config#custom"

BUTTONS = {"1#Action Items": "List the actions", "2#Summary": "Summarize"}
SHIPPED = {"1#SUMMARIZE": "Summarize this meeting", "2#ACTIONS": "What were the action items?"}


class FakeConfigurationTable:
    """``get_item``/``put_item`` over a dict, with the conditions in use."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.requests: List[Dict[str, Any]] = []

    def get_item(self, **request: Any) -> Dict[str, Any]:
        self.requests.append({"op": "get_item", **request})
        item = self.items.get(request["Key"][codec.KEY_ATTRIBUTE]["S"])
        return {"Item": item} if item is not None else {}

    def put_item(self, **request: Any) -> Dict[str, Any]:
        self.requests.append({"op": "put_item", **request})
        item = request["Item"]
        key = item[codec.KEY_ATTRIBUTE]["S"]
        condition = request.get("ConditionExpression")
        refused = (condition == "attribute_not_exists(#key)" and key in self.items) or (
            condition == "attribute_not_exists(#version)" and "Version" in self.items.get(key, {})
        )
        if condition == "#version = :expected":
            expected = request["ExpressionAttributeValues"][":expected"]["N"]
            refused = self.items.get(key, {}).get("Version", {}).get("N") != expected
        if refused:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "refused"}},
                "PutItem",
            )
        self.items[key] = item
        return {}

    # ── helpers ─────────────────────────────────────────────────────────────

    def seed(self, key: str, body: Dict[str, Any], *, version: int = 1, **metadata: Any) -> None:
        item = {
            codec.KEY_ATTRIBUTE: {"S": key},
            "Version": {"N": str(version)},
            "Body": {"B": codec.encode_body(body)},
        }
        for name, value in metadata.items():
            item[name] = {"S": value}
        self.items[key] = item

    def body(self, key: str = CUSTOM_KEY) -> Dict[str, Any]:
        return codec.decode_body(self.items[key]["Body"]["B"])

    def stored(self, key: str = CUSTOM_KEY) -> Dict[str, Any]:
        return self.body(key)[SECTION]

    def stored_metadata(self, key: str = CUSTOM_KEY) -> Dict[str, Any]:
        return codec.split_item(codec.unmarshal_item(self.items[key]))[0]

    def wrote_anything(self) -> bool:
        return any(request["op"] == "put_item" for request in self.requests)


@pytest.fixture()
def table(monkeypatch: pytest.MonkeyPatch) -> FakeConfigurationTable:
    """The baseline, a profile holding another page's edit, and the pointer."""
    fake = FakeConfigurationTable()
    fake.seed(DEFAULT_KEY, {SECTION: SHIPPED, "nova_sonic_config": {"voiceId": "tiffany"}})
    fake.seed(CUSTOM_KEY, {"nova_sonic_config": {"voiceId": "matthew"}})
    fake.items[ACTIVE_POINTER_KEY] = {
        codec.KEY_ATTRIBUTE: {"S": ACTIVE_POINTER_KEY},
        "ActiveProfile": {"S": "custom"},
    }
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore("lma-configuration", client=fake))
    return fake


def save(
    config: object,
    config_id: str = "CustomChatButtonConfig",
    identity: object = ADMIN_IDENTITY,
    expected_version: object = None,
    raw: str | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ChatButtonConfigId": config_id,
        "ButtonConfig": raw if raw is not None else json.dumps(config),
    }
    if expected_version is not None:
        payload["ExpectedVersion"] = expected_version
    return index.lambda_handler(
        {
            "identity": identity,
            "arguments": {"input": payload},
            "info": {"fieldName": "updateChatButtonConfig"},
        },
        None,
    )


def read_raw(config_id: str, identity: object = USER_IDENTITY) -> Dict[str, Any]:
    return index.lambda_handler(
        {
            "identity": identity,
            "arguments": {"ChatButtonConfigId": config_id},
            "info": {"fieldName": "getChatButtonConfig"},
        },
        None,
    )


def read(config_id: str, identity: object = USER_IDENTITY) -> Dict[str, Any]:
    return json.loads(read_raw(config_id, identity)["ChatButtonConfigId"])


def buttons_in(payload: Dict[str, Any]) -> Dict[str, Any]:
    """What the chat panel keeps of a read: its own "N#Label" filter."""
    return {key: value for key, value in payload.items() if index.BUTTON_FIELD_PATTERN.match(key)}


def refuse(identity: object, table: FakeConfigurationTable) -> None:
    """Assert the resolver refuses a save from `identity` and writes nothing."""
    with pytest.raises(Exception, match="Unauthorized"):
        save(BUTTONS, identity=identity)
    assert not table.wrote_anything()


# ── stored shape ────────────────────────────────────────────────────────────


def test_the_buttons_are_stored_under_their_sequence_keys(table: FakeConfigurationTable) -> None:
    response = save(BUTTONS)

    assert response == {
        "ChatButtonConfigId": "CustomChatButtonConfig",
        "Success": True,
        "Version": 2,
    }
    assert table.stored() == BUTTONS


def test_a_save_leaves_the_other_pages_settings_alone(table: FakeConfigurationTable) -> None:
    save(BUTTONS)

    assert table.body()["nova_sonic_config"] == {"voiceId": "matthew"}


def test_keys_outside_the_sequence_form_are_filtered_out(table: FakeConfigurationTable) -> None:
    save({**BUTTONS, "ChatButtonConfigId": "x", "Owner": "someone", "#Bare": "y", "1x#Bad": "z"})

    assert table.stored() == BUTTONS


def test_an_empty_configuration_clears_the_custom_buttons(table: FakeConfigurationTable) -> None:
    """Reset to Defaults saves `{}`; the shipped buttons then apply."""
    save(BUTTONS)
    save({})

    assert table.stored() == {}
    assert buttons_in(read("CustomChatButtonConfig")) == {}


def test_only_the_custom_record_can_be_written(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Only CustomChatButtonConfig"):
        save(BUTTONS, config_id="DefaultChatButtonConfig")
    assert not table.wrote_anything()


def test_an_arbitrary_partition_key_is_refused(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Only CustomChatButtonConfig"):
        save(BUTTONS, config_id="SomeOtherRecord")
    assert not table.wrote_anything()


def test_malformed_json_is_rejected(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Invalid JSON"):
        save(None, raw="{not json")
    assert not table.wrote_anything()


def test_a_json_array_is_rejected(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="must be a JSON object"):
        save([1, 2, 3])


# ── the read ────────────────────────────────────────────────────────────────


def test_the_default_id_reads_the_shipped_buttons(table: FakeConfigurationTable) -> None:
    assert buttons_in(read("DefaultChatButtonConfig")) == SHIPPED


def test_the_custom_id_reads_the_profiles_buttons_unmerged(table: FakeConfigurationTable) -> None:
    """A custom list replaces the shipped one, so the two are never merged here."""
    save({"1#Mine": "my prompt"})

    assert buttons_in(read("CustomChatButtonConfig")) == {"1#Mine": "my prompt"}


def test_a_profile_without_the_section_reads_as_no_buttons(table: FakeConfigurationTable) -> None:
    assert buttons_in(read("CustomChatButtonConfig")) == {}


def test_the_read_carries_the_version_the_dialog_saves_against(
    table: FakeConfigurationTable,
) -> None:
    version = save(BUTTONS)["Version"]

    assert read("CustomChatButtonConfig")["Version"] == version


def test_the_read_carries_only_the_buttons_and_the_version(table: FakeConfigurationTable) -> None:
    """Every signed-in user reads this record, so the response is the buttons.

    The stored profile also holds who saved it last, and other pages' sections;
    none of them is part of what a reader of the chat buttons asked for.
    """
    save(BUTTONS)

    assert read("CustomChatButtonConfig") == {**BUTTONS, "Version": 2}


def test_no_address_from_the_audit_attributes_reaches_a_reader(
    table: FakeConfigurationTable,
) -> None:
    """Pinned on the serialized response, not just the decoded map."""
    save(BUTTONS)

    response = json.dumps(read_raw("CustomChatButtonConfig"))
    assert "admin@example.com" not in response
    assert "UpdatedBy" not in response
    assert "UpdatedAt" not in response


def test_a_button_stored_by_hand_in_another_form_is_not_returned(
    table: FakeConfigurationTable,
) -> None:
    """The read applies the same "N#Label" form the write does."""
    table.seed(
        CUSTOM_KEY,
        {SECTION: {"1#Summary": "Summarize", "Summary": "x", "#Bare": "y", "1.5#Odd": "z"}},
    )

    assert read("CustomChatButtonConfig") == {"1#Summary": "Summarize", "Version": 1}


@pytest.mark.parametrize("identity", [USER_IDENTITY, ADMIN_IDENTITY, IAM_IDENTITY])
def test_every_signed_in_reader_is_served(
    identity: Dict[str, Any], table: FakeConfigurationTable
) -> None:
    """The chat panel loads its buttons for whoever is viewing a meeting."""
    assert buttons_in(read("DefaultChatButtonConfig", identity)) == SHIPPED


def test_the_read_is_strongly_consistent(table: FakeConfigurationTable) -> None:
    read("CustomChatButtonConfig")

    (get,) = [request for request in table.requests if request["op"] == "get_item"]
    assert get["ConsistentRead"] is True


def test_an_unrecognised_id_is_refused_rather_than_answered_with_the_defaults(
    table: FakeConfigurationTable,
) -> None:
    with pytest.raises(Exception, match="Unknown ChatButtonConfigId"):
        read("SomethingElse")


def test_a_request_naming_no_field_is_refused(table: FakeConfigurationTable) -> None:
    with pytest.raises(ValueError, match="this resolver serves"):
        index.lambda_handler(
            {"identity": ADMIN_IDENTITY, "arguments": {"ChatButtonConfigId": "x"}}, None
        )
    assert not table.wrote_anything()


@pytest.mark.parametrize("key", ["1#Summary", "12#Summary", "0#Summary", "3#", "12#A#B"])
def test_a_key_in_the_sequence_form_is_a_button(key: str) -> None:
    assert index.BUTTON_FIELD_PATTERN.match(key)


@pytest.mark.parametrize(
    "key",
    [
        "ChatButtonConfigId",
        "UpdatedBy",
        "#Summary",
        "1x#Summary",
        " 1#Summary",
        "Summary",
        "",
        "١#Summary",
    ],
)
def test_a_key_in_any_other_form_is_not_a_button(key: str) -> None:
    """Including a non-ASCII digit ahead of the separator."""
    assert not index.BUTTON_FIELD_PATTERN.match(key)


# ── concurrent saves ────────────────────────────────────────────────────────


def test_a_save_against_the_version_the_dialog_loaded_succeeds(
    table: FakeConfigurationTable,
) -> None:
    version = read("CustomChatButtonConfig", ADMIN_IDENTITY)["Version"]

    assert save(BUTTONS, expected_version=version)["Version"] == version + 1


def test_a_save_against_a_superseded_version_is_refused_with_a_reload_message(
    table: FakeConfigurationTable,
) -> None:
    loaded = read("CustomChatButtonConfig", ADMIN_IDENTITY)["Version"]
    save({"1#Theirs": "their prompt"})  # another administrator, in between

    with pytest.raises(Exception, match="^Conflict: .*reload"):
        save(BUTTONS, expected_version=loaded)
    assert table.stored() == {"1#Theirs": "their prompt"}


# ── group membership ────────────────────────────────────────────────────────


def test_a_member_of_the_admin_group_is_admitted(table: FakeConfigurationTable) -> None:
    assert save(BUTTONS, identity=ADMIN_IDENTITY)["Success"] is True


def test_a_single_group_claim_sent_as_a_string_is_admitted(
    table: FakeConfigurationTable,
) -> None:
    """AppSync delivers cognito:groups as a bare string for a one-group caller."""
    assert save(BUTTONS, identity={"claims": {"cognito:groups": "Admin"}})["Success"] is True


def test_a_comma_joined_group_claim_is_admitted(table: FakeConfigurationTable) -> None:
    assert save(BUTTONS, identity={"claims": {"cognito:groups": "Users,Admin"}})["Success"] is True


@pytest.mark.parametrize(
    "identity",
    [
        {"claims": {"cognito:groups": ["Users"]}},
        {"claims": {"cognito:groups": []}},
        {"claims": {"cognito:groups": "users"}},
        {"claims": {"cognito:groups": "SuperAdmins"}},
        IAM_IDENTITY,
    ],
)
def test_a_caller_outside_the_admin_group_is_refused(
    identity: dict, table: FakeConfigurationTable
) -> None:
    refuse(identity, table)


def test_a_refusal_is_logged_without_the_caller_s_email_address(
    capsys: pytest.CaptureFixture, table: FakeConfigurationTable
) -> None:
    """The log line identifies the caller by Cognito subject.

    An address in CloudWatch would outlive the decision it explains, on a
    retention period set deployment-wide. UpdatedBy on the stored item is the
    durable record of who changed a setting, and is where the address belongs.
    """
    refuse(
        {
            "claims": {
                "cognito:groups": ["Users"],
                "email": "someone@example.com",
                "cognito:username": "someone@example.com",
                "sub": "11111111-2222",
            }
        },
        table,
    )

    logged = capsys.readouterr().out
    assert "someone@example.com" not in logged
    assert "11111111-2222" in logged


def test_the_written_record_keeps_the_email_address(
    capsys: pytest.CaptureFixture, table: FakeConfigurationTable
) -> None:
    """The counterpart: the address is not dropped, only kept out of the log."""
    save(BUTTONS, identity=ADMIN_IDENTITY)

    assert table.stored_metadata()["UpdatedBy"] == "admin@example.com"
    assert "admin@example.com" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"claims": {}},
        {"claims": None},
        {"claims": {"sub": "u1"}},
        {"claims": {"cognito:groups": 17}},
        None,
    ],
)
def test_a_request_with_no_readable_group_claim_is_refused(
    identity: object, table: FakeConfigurationTable
) -> None:
    """Absent or unreadable group membership is refused, not treated as unrestricted."""
    refuse(identity, table)


def test_group_membership_in_the_arguments_does_not_admit_a_caller(
    table: FakeConfigurationTable,
) -> None:
    """Only the identity block is consulted, so a claim in the input carries nothing."""
    with pytest.raises(Exception, match="Unauthorized"):
        index.lambda_handler(
            {
                "identity": {"claims": {"cognito:groups": ["Users"]}},
                "arguments": {
                    "input": {
                        "ChatButtonConfigId": "CustomChatButtonConfig",
                        "ButtonConfig": json.dumps(BUTTONS),
                        "cognito:groups": ["Admin"],
                    }
                },
                "info": {"fieldName": "updateChatButtonConfig"},
            },
            None,
        )
    assert not table.wrote_anything()


# ── audit attributes ────────────────────────────────────────────────────────


def test_the_written_record_names_the_identity_that_saved_it(
    table: FakeConfigurationTable,
) -> None:
    save(BUTTONS)

    metadata = table.stored_metadata()
    assert metadata["UpdatedBy"] == "admin@example.com"
    assert metadata["UpdatedAt"].endswith("Z")


def test_the_subject_identifies_the_saver_when_no_email_claim_is_present(
    table: FakeConfigurationTable,
) -> None:
    save(BUTTONS, identity={"claims": {"cognito:groups": ["Admin"], "sub": "11111111-2222"}})

    assert table.stored_metadata()["UpdatedBy"] == "11111111-2222"


def test_audit_attributes_come_from_the_identity_not_the_request(
    table: FakeConfigurationTable,
) -> None:
    save({**BUTTONS, "UpdatedBy": "someone-else@example.com"})

    assert table.stored_metadata()["UpdatedBy"] == "admin@example.com"
    assert "UpdatedBy" not in table.stored()


def test_a_refused_save_is_logged_without_the_previous_saver_s_address(
    capsys: pytest.CaptureFixture, table: FakeConfigurationTable
) -> None:
    """The refusal names who saved first; that goes to the admin, not to CloudWatch."""
    loaded = read("CustomChatButtonConfig", ADMIN_IDENTITY)["Version"]
    save({"1#Theirs": "their prompt"}, identity=OTHER_ADMIN)
    capsys.readouterr()

    with pytest.raises(Exception, match="^Conflict: .*someone-else@example.com"):
        save(BUTTONS, expected_version=loaded)

    assert "someone-else@example.com" not in capsys.readouterr().out


def test_a_comment_on_the_save_is_passed_to_the_revision_it_records(
    table: FakeConfigurationTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: Dict[str, Any] = {}

    def write_section(*_args: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return 2

    monkeypatch.setattr(index.CONFIG_STORE, "write_section", write_section)

    index.lambda_handler(
        {
            "identity": ADMIN_IDENTITY,
            "arguments": {
                "input": {
                    "ChatButtonConfigId": "CustomChatButtonConfig",
                    "ButtonConfig": "{}",
                    "Comment": "why",
                }
            },
            "info": {"fieldName": "updateChatButtonConfig"},
        },
        None,
    )

    assert seen["comment"] == "why"
