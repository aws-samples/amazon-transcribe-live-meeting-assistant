# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the Nova Sonic configuration resolver (no AWS calls).

This resolver serves both halves of the Nova Sonic Configuration page -- the read
that fills the form and the write that saves it -- from the shared
configuration table. The behaviours asserted here are the ones that are silent
when wrong:

* **Only the allowed fields are stored**, with enum values checked and the
  translator text bounded, so the voice assistant reads settings it can use.
* **A save replaces this page's section and nothing else**, because the one
  profile holds every configuration page's settings.
* **A concurrent save is refused, not applied.** The page sends the version it
  loaded and a stale one is rejected with a message to reload.
* **Group membership is re-checked here**, not only on the AppSync field; the
  read additionally admits an IAM-signed request, which is the field's second
  authorization mode. Refusals are logged by Cognito subject, not by address.
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
# source/lambda_functions/update_nova_sonic_config_resolver -> source
SOURCE = HERE.parents[1]
LAYER = SOURCE / "lambda_layers" / "configuration_layer"
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
    "update_nova_sonic_config_resolver_index", HERE / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

ADMIN_IDENTITY = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}
OTHER_ADMIN = {"claims": {"cognito:groups": ["Admin"], "email": "someone-else@example.com"}}
IAM_IDENTITY = {
    "accountId": "123456789012",
    "userArn": "arn:aws:sts::123456789012:assumed-role/a-service-role/session",
    "username": "AROAEXAMPLE:session",
}

SECTION = "nova_sonic_config"
CUSTOM_KEY = "Config#custom"

CONFIG = {"systemPrompt": "Be brief", "promptMode": "inject", "meetingMode": "normal"}

SHIPPED = {"systemPrompt": "You are Alex.", "voiceId": "tiffany", "meetingMode": "normal"}


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
    """A table holding the baseline, a profile with a summary edit, and the pointer."""
    fake = FakeConfigurationTable()
    fake.seed(DEFAULT_KEY, {SECTION: SHIPPED, "summary_prompt_templates": {"1#S": "shipped"}})
    fake.seed(CUSTOM_KEY, {"summary_prompt_templates": {"1#S": "an admin's prompt"}})
    fake.items[ACTIVE_POINTER_KEY] = {
        codec.KEY_ATTRIBUTE: {"S": ACTIVE_POINTER_KEY},
        "ActiveProfile": {"S": "custom"},
    }
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore("lma-configuration", client=fake))
    return fake


def save(
    config: object,
    config_id: str = "CustomNovaSonicConfig",
    identity: object = ADMIN_IDENTITY,
    expected_version: object = None,
    raw: str | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "NovaSonicConfigId": config_id,
        "ConfigData": raw if raw is not None else json.dumps(config),
    }
    if expected_version is not None:
        payload["ExpectedVersion"] = expected_version
    return index.lambda_handler(
        {
            "identity": identity,
            "arguments": {"input": payload},
            "info": {"fieldName": "updateNovaSonicConfig"},
        },
        None,
    )


def read(config_id: str, identity: object = ADMIN_IDENTITY) -> Dict[str, Any]:
    response = index.lambda_handler(
        {
            "identity": identity,
            "arguments": {"NovaSonicConfigId": config_id},
            "info": {"fieldName": "getNovaSonicConfig"},
        },
        None,
    )
    return json.loads(response["NovaSonicConfigId"])


def refuse(identity: object, table: FakeConfigurationTable) -> None:
    """Assert the resolver refuses a save from `identity` and writes nothing."""
    with pytest.raises(Exception, match="Unauthorized"):
        save(CONFIG, identity=identity)
    assert not table.wrote_anything()


# ── stored shape ────────────────────────────────────────────────────────────


def test_the_known_fields_are_stored(table: FakeConfigurationTable) -> None:
    response = save(CONFIG)

    assert response == {"NovaSonicConfigId": "CustomNovaSonicConfig", "Success": True, "Version": 2}
    assert table.stored() == CONFIG


def test_a_save_leaves_the_other_pages_settings_alone(table: FakeConfigurationTable) -> None:
    """The profile holds the summary prompts too; replacing the body would lose them."""
    save(CONFIG)

    assert table.body()["summary_prompt_templates"] == {"1#S": "an admin's prompt"}


def test_fields_outside_the_allow_list_are_filtered_out(table: FakeConfigurationTable) -> None:
    save({"systemPrompt": "Be brief", "ExpiresAfter": 1, "Owner": "someone"})

    assert table.stored() == {"systemPrompt": "Be brief"}


def test_every_specially_validated_field_is_also_on_the_allow_list() -> None:
    """A field validated but not allowed would be dropped before its check ran."""
    specialised = set(index.ENUM_FIELDS) | set(index.BOUNDED_TEXT_FIELDS)

    assert specialised <= index.ALLOWED_FIELDS, specialised - index.ALLOWED_FIELDS


def test_the_seeder_adopts_exactly_the_fields_this_resolver_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upgrade carries across what the page could have saved, and no more.

    A field accepted here but not adopted would be an edit the upgrade loses; a
    field adopted but not accepted here would be one the page could never
    change again.
    """
    seeder_dir = SOURCE / "lambda_functions" / "configuration_seeder"
    monkeypatch.syspath_prepend(str(seeder_dir))
    spec = importlib.util.spec_from_file_location(
        "configuration_seeder_under_test", seeder_dir / "lambda_function.py"
    )
    seeder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seeder)

    assert set(seeder.NOVA_SONIC_FIELDS) == index.ALLOWED_FIELDS


def test_an_unrecognised_enum_value_is_dropped_rather_than_stored(
    table: FakeConfigurationTable,
) -> None:
    save({"systemPrompt": "Be brief", "promptMode": "not-a-mode"})

    assert table.stored() == {"systemPrompt": "Be brief"}


def test_a_translator_label_longer_than_the_bound_is_trimmed_to_it(
    table: FakeConfigurationTable,
) -> None:
    save({"meetingMode": "translator", "translatorLanguageA": "x" * 500})

    assert table.stored()["translatorLanguageA"] == "x" * index.MAX_LANGUAGE_LABEL_LEN


def test_a_translator_label_that_is_not_a_string_is_dropped(
    table: FakeConfigurationTable,
) -> None:
    save({"meetingMode": "translator", "translatorLanguageA": 17})

    assert "translatorLanguageA" not in table.stored()


def test_the_legacy_group_flag_is_stored_as_a_boolean(table: FakeConfigurationTable) -> None:
    save({"groupMeetingMode": 1})

    assert table.stored() == {"groupMeetingMode": True}


def test_a_reset_stores_an_empty_section(table: FakeConfigurationTable) -> None:
    """Reset to Defaults saves `{}`; the baseline then supplies every field."""
    save(CONFIG)
    save({})

    assert table.stored() == {}
    assert index.CONFIG_STORE.get_section(SECTION).values == SHIPPED


def test_only_the_custom_record_can_be_written(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Only CustomNovaSonicConfig"):
        save(CONFIG, config_id="DefaultNovaSonicConfig")
    assert not table.wrote_anything()


def test_malformed_json_is_rejected(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Invalid JSON"):
        save(None, raw="{not json")
    assert not table.wrote_anything()


def test_a_json_array_is_rejected(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="must be a JSON object"):
        save([1, 2, 3])


# ── the read ────────────────────────────────────────────────────────────────


def test_the_read_returns_the_shipped_settings_for_the_default_id(
    table: FakeConfigurationTable,
) -> None:
    payload = read("DefaultNovaSonicConfig")

    assert {key: payload[key] for key in SHIPPED} == SHIPPED


def test_the_read_returns_the_profiles_section_and_its_version(
    table: FakeConfigurationTable,
) -> None:
    version = save(CONFIG)["Version"]

    payload = read("CustomNovaSonicConfig")

    assert payload["Version"] == version
    assert payload["UpdatedBy"] == "admin@example.com"
    assert {key: payload[key] for key in CONFIG} == CONFIG
    assert "summary_prompt_templates" not in payload


def test_the_read_of_a_profile_without_the_section_is_empty_settings(
    table: FakeConfigurationTable,
) -> None:
    """The page then shows every field as "use the default"."""
    payload = read("CustomNovaSonicConfig")

    assert set(payload) - {"Version", "UpdatedBy", "UpdatedAt"} == set()
    assert payload["Version"] == 1


def test_the_read_is_strongly_consistent(table: FakeConfigurationTable) -> None:
    """The page has to show what is stored, straight after a save."""
    read("CustomNovaSonicConfig")

    (get,) = [request for request in table.requests if request["op"] == "get_item"]
    assert get["ConsistentRead"] is True


def test_an_unrecognised_id_is_refused_rather_than_answered_with_the_baseline(
    table: FakeConfigurationTable,
) -> None:
    with pytest.raises(Exception, match="Unknown NovaSonicConfigId"):
        read("SomethingElse")


def test_an_iam_signed_read_is_served(table: FakeConfigurationTable) -> None:
    """The field's second authorization mode; AppSync has already checked the signer."""
    assert read("DefaultNovaSonicConfig", identity=IAM_IDENTITY)["voiceId"] == "tiffany"


def test_a_user_pool_read_from_outside_the_admin_group_is_refused(
    table: FakeConfigurationTable,
) -> None:
    with pytest.raises(Exception, match="Unauthorized"):
        read("CustomNovaSonicConfig", identity={"claims": {"cognito:groups": ["Users"]}})


def test_an_iam_signed_save_is_refused(table: FakeConfigurationTable) -> None:
    """The mutation is a user-pool Admin operation only."""
    refuse(IAM_IDENTITY, table)


def test_a_request_naming_no_field_is_refused(table: FakeConfigurationTable) -> None:
    """Guessing read-or-write from the argument shape is not done."""
    with pytest.raises(ValueError, match="this resolver serves"):
        index.lambda_handler(
            {"identity": ADMIN_IDENTITY, "arguments": {"NovaSonicConfigId": "x"}}, None
        )
    assert not table.wrote_anything()


# ── concurrent saves ────────────────────────────────────────────────────────


def test_a_save_against_the_version_the_page_loaded_succeeds(
    table: FakeConfigurationTable,
) -> None:
    version = read("CustomNovaSonicConfig")["Version"]

    assert save(CONFIG, expected_version=version)["Version"] == version + 1


def test_a_save_against_a_superseded_version_is_refused_with_a_reload_message(
    table: FakeConfigurationTable,
) -> None:
    loaded = read("CustomNovaSonicConfig")["Version"]
    save({"voiceId": "matthew"})  # another administrator, in between

    with pytest.raises(Exception, match="^Conflict: .*reload"):
        save(CONFIG, expected_version=loaded)
    assert table.stored() == {"voiceId": "matthew"}


def test_a_save_without_a_version_is_still_conditional_on_what_it_read(
    table: FakeConfigurationTable,
) -> None:
    save(CONFIG)

    put = [request for request in table.requests if request["op"] == "put_item"][-1]
    assert put["ConditionExpression"] == "#version = :expected"


# ── group membership ────────────────────────────────────────────────────────


def test_a_member_of_the_admin_group_is_admitted(table: FakeConfigurationTable) -> None:
    assert save(CONFIG, identity=ADMIN_IDENTITY)["Success"] is True


def test_a_single_group_claim_sent_as_a_string_is_admitted(
    table: FakeConfigurationTable,
) -> None:
    """AppSync delivers cognito:groups as a bare string for a one-group caller."""
    assert save(CONFIG, identity={"claims": {"cognito:groups": "Admin"}})["Success"] is True


def test_a_comma_joined_group_claim_is_admitted(table: FakeConfigurationTable) -> None:
    assert save(CONFIG, identity={"claims": {"cognito:groups": "Users,Admin"}})["Success"] is True


@pytest.mark.parametrize(
    "identity",
    [
        {"claims": {"cognito:groups": ["Users"]}},
        {"claims": {"cognito:groups": []}},
        {"claims": {"cognito:groups": "users"}},
        {"claims": {"cognito:groups": "SuperAdmins"}},
    ],
)
def test_a_caller_outside_the_admin_group_is_refused(
    identity: dict, table: FakeConfigurationTable
) -> None:
    refuse(identity, table)


def test_a_user_pool_identity_carrying_a_user_arn_is_not_treated_as_iam(
    table: FakeConfigurationTable,
) -> None:
    """Only an identity with no user-pool claims at all is read as IAM-signed."""
    with pytest.raises(Exception, match="Unauthorized"):
        read(
            "CustomNovaSonicConfig",
            identity={"claims": {"cognito:groups": ["Users"]}, "userArn": "arn:aws:iam::x"},
        )


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
    save(CONFIG, identity=ADMIN_IDENTITY)

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
                        "NovaSonicConfigId": "CustomNovaSonicConfig",
                        "ConfigData": json.dumps(CONFIG),
                        "cognito:groups": ["Admin"],
                    }
                },
                "info": {"fieldName": "updateNovaSonicConfig"},
            },
            None,
        )
    assert not table.wrote_anything()


# ── audit attributes ────────────────────────────────────────────────────────


def test_the_written_record_names_the_identity_that_saved_it(
    table: FakeConfigurationTable,
) -> None:
    save(CONFIG)

    metadata = table.stored_metadata()
    assert metadata["UpdatedBy"] == "admin@example.com"
    assert metadata["UpdatedAt"].endswith("Z")


def test_the_subject_identifies_the_saver_when_no_email_claim_is_present(
    table: FakeConfigurationTable,
) -> None:
    save(CONFIG, identity={"claims": {"cognito:groups": ["Admin"], "sub": "11111111-2222"}})

    assert table.stored_metadata()["UpdatedBy"] == "11111111-2222"


def test_audit_attributes_come_from_the_identity_not_the_request(
    table: FakeConfigurationTable,
) -> None:
    save({**CONFIG, "UpdatedBy": "someone-else@example.com"})

    assert table.stored_metadata()["UpdatedBy"] == "admin@example.com"
    assert "UpdatedBy" not in table.stored()


def test_a_refused_save_is_logged_without_the_previous_saver_s_address(
    capsys: pytest.CaptureFixture, table: FakeConfigurationTable
) -> None:
    """The refusal names who saved first; that goes to the admin, not to CloudWatch."""
    loaded = read("CustomNovaSonicConfig")["Version"]
    save({"voiceId": "matthew"}, identity=OTHER_ADMIN)
    capsys.readouterr()

    with pytest.raises(Exception, match="^Conflict: .*someone-else@example.com"):
        save(CONFIG, expected_version=loaded)

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
                    "NovaSonicConfigId": "CustomNovaSonicConfig",
                    "ConfigData": "{}",
                    "Comment": "why",
                }
            },
            "info": {"fieldName": "updateNovaSonicConfig"},
        },
        None,
    )

    assert seen["comment"] == "why"
