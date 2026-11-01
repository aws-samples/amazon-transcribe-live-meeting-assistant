# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the ASR runtime-switch resolver (no AWS calls).

The resolver serves updateAsrConfig's save and getAsrConfig's read for the Stream
Audio and Upload Audio forms' read, and the Virtual Participant's SigV4 read,
from the shared configuration table. Asserted here:

* **The read answers with the effective switches** -- the shipped baseline with
  the operator's profile over it -- as booleans, plus the profile's version.
* **Only the three switches are stored**, parsed rather than truth-tested, and a
  save replaces this page's section and nothing else.
* **A concurrent save is refused, not applied.**
* **The write is for the Admin group**, re-checked here; the read is not
  restricted, because the streaming forms and the Virtual Participant need it.
* **The audit attributes come from the verified identity.**
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
# source/lambda_functions/update_asr_config_resolver -> source
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
    "update_asr_config_resolver_index", HERE / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

ADMIN_IDENTITY = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}
OTHER_ADMIN = {"claims": {"cognito:groups": ["Admin"], "email": "someone-else@example.com"}}
USER_IDENTITY = {"claims": {"cognito:groups": ["Users"], "email": "user@example.com"}}
IAM_IDENTITY = {
    "accountId": "123456789012",
    "userArn": "arn:aws:sts::123456789012:assumed-role/vp-task-role/session",
}

SECTION = "asr_engine"
CUSTOM_KEY = "Config#custom"
SHIPPED = {
    "streamingEngineMicrovm": False,
    "virtualParticipantEngineMicrovm": False,
    "diarizeVirtualParticipant": False,
}
ALL_ON = {key: True for key in SHIPPED}


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

    def seed(self, key: str, body: Dict[str, Any], *, version: int = 1) -> None:
        self.items[key] = {
            codec.KEY_ATTRIBUTE: {"S": key},
            "Version": {"N": str(version)},
            "Body": {"B": codec.encode_body(body)},
        }

    def body(self, key: str = CUSTOM_KEY) -> Dict[str, Any]:
        return codec.decode_body(self.items[key]["Body"]["B"])

    def stored(self) -> Dict[str, Any]:
        return self.body()[SECTION]

    def stored_metadata(self) -> Dict[str, Any]:
        return codec.split_item(codec.unmarshal_item(self.items[CUSTOM_KEY]))[0]

    def wrote_anything(self) -> bool:
        return any(request["op"] == "put_item" for request in self.requests)


@pytest.fixture()
def table(monkeypatch: pytest.MonkeyPatch) -> FakeConfigurationTable:
    fake = FakeConfigurationTable()
    fake.seed(DEFAULT_KEY, {SECTION: SHIPPED, "chat_buttons": {"1#A": "a"}})
    fake.seed(CUSTOM_KEY, {"chat_buttons": {"1#Mine": "mine"}})
    fake.items[ACTIVE_POINTER_KEY] = {
        codec.KEY_ATTRIBUTE: {"S": ACTIVE_POINTER_KEY},
        "ActiveProfile": {"S": "custom"},
    }
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore("lma-configuration", client=fake))
    return fake


def save(
    config: object,
    config_id: str = "CustomAsrConfig",
    identity: object = ADMIN_IDENTITY,
    expected_version: object = None,
    raw: str | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "AsrConfigId": config_id,
        "ConfigData": raw if raw is not None else json.dumps(config),
    }
    if expected_version is not None:
        payload["ExpectedVersion"] = expected_version
    return index.lambda_handler(
        {
            "identity": identity,
            "arguments": {"input": payload},
            "info": {"fieldName": "updateAsrConfig"},
        },
        None,
    )


def read(config_id: str = "CustomAsrConfig", identity: object = USER_IDENTITY) -> Dict[str, Any]:
    return index.lambda_handler(
        {
            "identity": identity,
            "arguments": {"AsrConfigId": config_id},
            "info": {"fieldName": "getAsrConfig"},
        },
        None,
    )


def refuse(identity: object, table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Unauthorized"):
        save({"streamingEngineMicrovm": True}, identity=identity)
    assert not table.wrote_anything()


# ── stored shape ────────────────────────────────────────────────────────────


def test_the_switches_are_stored_as_booleans(table: FakeConfigurationTable) -> None:
    response = save(
        {
            "streamingEngineMicrovm": True,
            "virtualParticipantEngineMicrovm": False,
            "diarizeVirtualParticipant": False,
        }
    )

    assert response == {"AsrConfigId": "CustomAsrConfig", "Success": True, "Version": 2}
    assert table.stored() == {
        "streamingEngineMicrovm": True,
        "virtualParticipantEngineMicrovm": False,
        "diarizeVirtualParticipant": False,
    }


def test_a_save_leaves_the_other_pages_settings_alone(table: FakeConfigurationTable) -> None:
    save({"streamingEngineMicrovm": True})

    assert table.body()["chat_buttons"] == {"1#Mine": "mine"}


def test_string_booleans_from_an_older_client_are_parsed_not_truth_tested(
    table: FakeConfigurationTable,
) -> None:
    # bool("false") is True.
    save(
        {
            "streamingEngineMicrovm": "true",
            "virtualParticipantEngineMicrovm": "false",
            "diarizeVirtualParticipant": "FALSE",
        }
    )

    assert table.stored() == {
        "streamingEngineMicrovm": True,
        "virtualParticipantEngineMicrovm": False,
        "diarizeVirtualParticipant": False,
    }


def test_a_value_that_is_not_a_boolean_is_dropped_rather_than_coerced(
    table: FakeConfigurationTable,
) -> None:
    save(
        {
            "streamingEngineMicrovm": "yes",
            "virtualParticipantEngineMicrovm": 1,
            "diarizeVirtualParticipant": None,
        }
    )

    assert table.stored() == {}


def test_retired_tuning_fields_are_filtered_out(table: FakeConfigurationTable) -> None:
    """The threshold and its friends live in the image now; a stale client that
    still sends them must not be able to put a number back into the store."""
    save(
        {
            "streamingEngineMicrovm": True,
            "engineDefaultMicrovm": True,
            "speakerThreshold": 0.3,
            "minSegmentMs": 2500,
            "maxSpeakers": 4,
            "liveTurnCut": False,
            "modelId": "../../etc/passwd",
        }
    )

    assert table.stored() == {"streamingEngineMicrovm": True}


def test_only_the_custom_record_can_be_written(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Only CustomAsrConfig"):
        save({"streamingEngineMicrovm": True}, config_id="DefaultAsrConfig")
    assert not table.wrote_anything()


def test_malformed_json_is_rejected(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Invalid JSON"):
        save(None, raw="{not json")
    assert not table.wrote_anything()


def test_a_json_array_is_rejected(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="must be a JSON object"):
        save([1, 2, 3])


def test_the_allow_list_is_exactly_the_three_switches() -> None:
    """Adding a field here means adding it to the AppSync type, the transcriber's
    and the VP's readers and the asr_engine schema - fail loudly if it drifts."""
    assert index.ALLOWED_FIELDS == {
        "streamingEngineMicrovm",
        "virtualParticipantEngineMicrovm",
        "diarizeVirtualParticipant",
    }


def test_the_seeder_adopts_exactly_the_switches_this_resolver_accepts() -> None:
    """The upgrade carries across what the page could have saved, and no more."""
    seeder_dir = SOURCE / "lambda_functions" / "configuration_seeder"
    if str(seeder_dir) not in sys.path:
        sys.path.insert(0, str(seeder_dir))
    spec = importlib.util.spec_from_file_location(
        "configuration_seeder_for_asr_test", seeder_dir / "lambda_function.py"
    )
    seeder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seeder)

    assert set(seeder.ASR_ENGINE_FIELDS) == index.ALLOWED_FIELDS


# ── the read ────────────────────────────────────────────────────────────────


def test_a_fresh_deployment_reads_as_every_switch_off(table: FakeConfigurationTable) -> None:
    assert read() == {"AsrConfigId": "CustomAsrConfig", **SHIPPED, "Version": 1}


def test_the_read_is_the_profile_over_the_shipped_switches(table: FakeConfigurationTable) -> None:
    save({"streamingEngineMicrovm": True})

    assert read() == {
        "AsrConfigId": "CustomAsrConfig",
        **SHIPPED,
        "streamingEngineMicrovm": True,
        "Version": 2,
    }


def test_the_read_answers_only_the_switches_and_the_version(
    table: FakeConfigurationTable,
) -> None:
    """Nothing saying who saved the record reaches every signed-in reader."""
    table.seed(CUSTOM_KEY, {SECTION: {**ALL_ON, "speakerThreshold": 0.35}})

    response = read()

    assert response == {"AsrConfigId": "CustomAsrConfig", **ALL_ON, "Version": 1}
    assert "UpdatedBy" not in json.dumps(response)


@pytest.mark.parametrize("identity", [USER_IDENTITY, ADMIN_IDENTITY, IAM_IDENTITY])
def test_every_signed_in_reader_is_served(
    identity: Dict[str, Any], table: FakeConfigurationTable
) -> None:
    """The streaming forms read this for every user, the VP with SigV4."""
    assert read(identity=identity)["streamingEngineMicrovm"] is False


def test_the_read_is_strongly_consistent_and_uncached(table: FakeConfigurationTable) -> None:
    """The Virtual Participant's path has never cached; it still does not."""
    read()
    read()

    gets = [request for request in table.requests if request["op"] == "get_item"]
    assert len(gets) == 4
    assert all(request["ConsistentRead"] for request in gets)


def test_an_unrecognised_id_is_refused(table: FakeConfigurationTable) -> None:
    with pytest.raises(Exception, match="Unknown AsrConfigId"):
        read("DefaultAsrConfig")


def test_a_request_naming_no_field_is_refused(table: FakeConfigurationTable) -> None:
    with pytest.raises(ValueError, match="this resolver serves"):
        index.lambda_handler({"identity": ADMIN_IDENTITY, "arguments": {"AsrConfigId": "x"}}, None)
    assert not table.wrote_anything()


# ── concurrent saves ────────────────────────────────────────────────────────


def test_a_save_against_the_version_the_page_loaded_succeeds(
    table: FakeConfigurationTable,
) -> None:
    version = read()["Version"]

    assert (
        save({"streamingEngineMicrovm": True}, expected_version=version)["Version"] == version + 1
    )


def test_a_save_against_a_superseded_version_is_refused_with_a_reload_message(
    table: FakeConfigurationTable,
) -> None:
    loaded = read()["Version"]
    save({"diarizeVirtualParticipant": True})  # another administrator, in between

    with pytest.raises(Exception, match="^Conflict: .*reload"):
        save({"streamingEngineMicrovm": True}, expected_version=loaded)
    assert table.stored() == {"diarizeVirtualParticipant": True}


# ── group membership ────────────────────────────────────────────────────────


def test_a_member_of_the_admin_group_is_admitted(table: FakeConfigurationTable) -> None:
    assert save({"streamingEngineMicrovm": True})["Success"] is True


def test_a_single_group_claim_sent_as_a_string_is_admitted(
    table: FakeConfigurationTable,
) -> None:
    """AppSync delivers cognito:groups as a bare string for a one-group caller."""
    response = save(
        {"streamingEngineMicrovm": True},
        identity={"claims": {"cognito:groups": "Admin", "email": "admin@example.com"}},
    )

    assert response["Success"] is True


def test_a_comma_joined_group_claim_is_admitted(table: FakeConfigurationTable) -> None:
    response = save(
        {"streamingEngineMicrovm": True}, identity={"claims": {"cognito:groups": "Users,Admin"}}
    )

    assert response["Success"] is True


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
    """The log line identifies the caller by Cognito subject."""
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
    save({"streamingEngineMicrovm": True})

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
                        "AsrConfigId": "CustomAsrConfig",
                        "ConfigData": json.dumps({"streamingEngineMicrovm": True}),
                        "cognito:groups": ["Admin"],
                    }
                },
                "info": {"fieldName": "updateAsrConfig"},
            },
            None,
        )
    assert not table.wrote_anything()


# ── audit attributes ────────────────────────────────────────────────────────


def test_the_written_record_names_the_identity_that_saved_it(
    table: FakeConfigurationTable,
) -> None:
    save({"streamingEngineMicrovm": True})

    metadata = table.stored_metadata()
    assert metadata["UpdatedBy"] == "admin@example.com"
    assert metadata["UpdatedAt"].endswith("Z")


def test_the_subject_identifies_the_saver_when_no_email_claim_is_present(
    table: FakeConfigurationTable,
) -> None:
    save(
        {"streamingEngineMicrovm": True},
        identity={"claims": {"cognito:groups": ["Admin"], "sub": "11111111-2222"}},
    )

    assert table.stored_metadata()["UpdatedBy"] == "11111111-2222"


def test_audit_attributes_come_from_the_identity_not_the_request(
    table: FakeConfigurationTable,
) -> None:
    save({"streamingEngineMicrovm": True, "UpdatedBy": "someone-else@example.com"})

    assert table.stored_metadata()["UpdatedBy"] == "admin@example.com"
    assert "UpdatedBy" not in table.stored()


def test_a_refused_save_is_logged_without_the_previous_saver_s_address(
    capsys: pytest.CaptureFixture, table: FakeConfigurationTable
) -> None:
    """The refusal names who saved first; that goes to the admin, not to CloudWatch."""
    loaded = read()["Version"]
    save({"diarizeVirtualParticipant": True}, identity=OTHER_ADMIN)
    capsys.readouterr()

    with pytest.raises(Exception, match="^Conflict: .*someone-else@example.com"):
        save({"streamingEngineMicrovm": True}, expected_version=loaded)

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
                "input": {"AsrConfigId": "CustomAsrConfig", "ConfigData": "{}", "Comment": "why"}
            },
            "info": {"fieldName": "updateAsrConfig"},
        },
        None,
    )

    assert seen["comment"] == "why"
