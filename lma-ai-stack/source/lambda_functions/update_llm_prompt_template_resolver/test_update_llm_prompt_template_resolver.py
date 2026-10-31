# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the summary prompt templates resolver (no AWS calls).

This resolver serves both halves of the Transcript Summary page -- the read that
fills the editor and the write that saves it -- and the behaviours asserted
here are the ones that are silent when wrong:

* **A concurrent save is refused, not applied.** Two administrators with the
  page open used to mean the second save replaced the first with no trace. The
  editor sends the version it loaded and a stale one is rejected, so both the
  version travelling with the read and the refusal on the write are tested.
* **Only the allowed fields are stored**, so a saved record holds the prompt
  configuration and nothing else.
* **Group membership is re-checked here**, not only on the AppSync field, and
  the refusal is logged by Cognito subject rather than by email address.
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
# source/lambda_functions/update_llm_prompt_template_resolver -> source
LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
for path in (str(HERE), str(LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "lma-configuration")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from lma_config import codec  # noqa: E402
from lma_config.keys import ACTIVE_POINTER_KEY, DEFAULT_KEY  # noqa: E402
from lma_config.store import ConfigurationStore  # noqa: E402

# Loaded by path under a unique name: each config resolver's handler module is
# called `index.py`, so a plain `import index` would resolve to whichever one a
# shared test session imported first.
_SPEC = importlib.util.spec_from_file_location(
    "update_llm_prompt_template_resolver_index", HERE / "index.py"
)
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

ADMIN_IDENTITY = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}}

# Attributes the resolver stamps on every write; the field-level assertions below
# are about the configuration itself, so they are dropped first.
AUDIT_ATTRIBUTES = ("UpdatedBy", "UpdatedAt")

TEMPLATES = {"1#SUMMARY": "Summarize {transcript}", "2#ACTIONS": "List actions in {transcript}"}

SECTION = "summary_prompt_templates"
CUSTOM_KEY = "Config#custom"


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

    def stored_prompts(self, key: str = CUSTOM_KEY) -> Dict[str, Any]:
        return codec.decode_body(self.items[key]["Body"]["B"])[SECTION]

    def stored_metadata(self, key: str = CUSTOM_KEY) -> Dict[str, Any]:
        return codec.split_item(codec.unmarshal_item(self.items[key]))[0]

    def wrote_anything(self) -> bool:
        return any(request["op"] == "put_item" for request in self.requests)


def install(table: FakeConfigurationTable, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        index, "CONFIG_STORE", ConfigurationStore("lma-configuration", client=table)
    )


def fresh(monkeypatch: pytest.MonkeyPatch) -> FakeConfigurationTable:
    """A table holding the baseline, an empty profile and the active pointer."""
    table = FakeConfigurationTable()
    table.seed(DEFAULT_KEY, {SECTION: {"1#SUMMARY": "the shipped prompt"}})
    table.seed(CUSTOM_KEY, {SECTION: {}})
    table.items[ACTIVE_POINTER_KEY] = {
        codec.KEY_ATTRIBUTE: {"S": ACTIVE_POINTER_KEY},
        "ActiveProfile": {"S": "custom"},
    }
    install(table, monkeypatch)
    return table


def save(
    config: object,
    *,
    config_id: str = "CustomSummaryPromptTemplates",
    identity: object = ADMIN_IDENTITY,
    expected_version: object = 1,
) -> dict:
    payload: Dict[str, Any] = {
        "LLMPromptTemplateId": config_id,
        "TemplateConfig": json.dumps(config) if not isinstance(config, str) else config,
    }
    if expected_version is not None:
        payload["ExpectedVersion"] = expected_version
    return index.lambda_handler(
        {
            "identity": identity,
            "info": {"fieldName": "updateLLMPromptTemplate"},
            "arguments": {"input": payload},
        },
        None,
    )


def read(config_id: str, identity: object = ADMIN_IDENTITY) -> dict:
    response = index.lambda_handler(
        {
            "identity": identity,
            "info": {"fieldName": "getLLMPromptTemplate"},
            "arguments": {"LLMPromptTemplateId": config_id},
        },
        None,
    )
    return json.loads(response["LLMPromptTemplateId"])


def refuse(identity: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert the resolver refuses `identity` and writes nothing."""
    table = fresh(monkeypatch)
    with pytest.raises(Exception, match="Unauthorized"):
        save(TEMPLATES, identity=identity)
    assert not table.wrote_anything()


# ── the read that fills the editor ──────────────────────────────────────────


def test_the_read_returns_the_stored_prompts_for_the_requested_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = fresh(monkeypatch)
    table.seed(CUSTOM_KEY, {SECTION: TEMPLATES}, version=4)

    assert read("DefaultSummaryPromptTemplates")["1#SUMMARY"] == "the shipped prompt"
    custom = read("CustomSummaryPromptTemplates")
    assert {key: custom[key] for key in TEMPLATES} == TEMPLATES


def test_the_read_carries_the_version_the_editor_must_send_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without it the editor has nothing to condition its save on."""
    table = fresh(monkeypatch)
    table.seed(CUSTOM_KEY, {SECTION: TEMPLATES}, version=7)
    assert read("CustomSummaryPromptTemplates")["Version"] == 7


def test_the_read_reports_version_zero_for_a_record_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first save then conditions on the record being absent."""
    table = fresh(monkeypatch)
    del table.items[CUSTOM_KEY]
    assert read("CustomSummaryPromptTemplates") == {"Version": 0}


def test_the_read_carries_who_saved_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    table = fresh(monkeypatch)
    table.seed(
        CUSTOM_KEY,
        {SECTION: TEMPLATES},
        version=2,
        UpdatedBy="someone@example.com",
        UpdatedAt="2026-03-03T00:00:00Z",
    )
    payload = read("CustomSummaryPromptTemplates")
    assert payload["UpdatedBy"] == "someone@example.com"
    assert payload["UpdatedAt"] == "2026-03-03T00:00:00Z"


def test_the_read_handles_a_record_with_no_prompt_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompts at the top level of the body rather than under a section name.

    Nothing writes a record like this, so it stands for one repaired by hand in
    the DynamoDB console. Answering the editor with nothing would be the more
    confusing outcome.
    """
    table = fresh(monkeypatch)
    table.items[CUSTOM_KEY] = {
        codec.KEY_ATTRIBUTE: {"S": CUSTOM_KEY},
        "1#SUMMARY": {"S": "an inline prompt"},
        "*Information*": {"S": "guidance text"},
    }
    payload = read("CustomSummaryPromptTemplates")
    assert payload["1#SUMMARY"] == "an inline prompt"
    assert "*Information*" not in payload


def test_the_read_does_not_serve_a_cached_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """An editor must show what is stored, including a save made moments ago."""
    table = fresh(monkeypatch)
    read("CustomSummaryPromptTemplates")
    table.seed(CUSTOM_KEY, {SECTION: {"1#SUMMARY": "changed underneath"}}, version=2)
    assert read("CustomSummaryPromptTemplates")["1#SUMMARY"] == "changed underneath"


def test_the_read_refuses_an_identifier_it_does_not_recognise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not be answered with a plausible-looking record."""
    fresh(monkeypatch)
    with pytest.raises(Exception, match="Unknown LLMPromptTemplateId"):
        read("CustomSummaryPromptTemplate")


def test_a_request_naming_no_field_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guessing from the argument shape would mean a read treated as a write."""
    fresh(monkeypatch)
    with pytest.raises(Exception, match="named"):
        index.lambda_handler(
            {"identity": ADMIN_IDENTITY, "arguments": {"LLMPromptTemplateId": "x"}}, None
        )


# ── stored shape ────────────────────────────────────────────────────────────


def test_the_templates_are_stored_under_their_sequence_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = fresh(monkeypatch)
    response = save(TEMPLATES)

    assert response == {
        "LLMPromptTemplateId": "CustomSummaryPromptTemplates",
        "Success": True,
        "Version": 2,
    }
    assert table.stored_prompts() == TEMPLATES


def test_keys_outside_the_sequence_form_are_filtered_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = fresh(monkeypatch)
    save({"1#SUMMARY": "Summarize", "ExpiresAfter": 1, "Owner": "someone"})

    assert set(table.stored_prompts()) == {"1#SUMMARY"}


def test_an_empty_configuration_clears_the_custom_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resetting to defaults stores an empty section, so the reader falls back."""
    table = fresh(monkeypatch)
    save({})

    assert table.stored_prompts() == {}


def test_only_the_custom_record_can_be_written(monkeypatch: pytest.MonkeyPatch) -> None:
    table = fresh(monkeypatch)
    with pytest.raises(Exception, match="Only CustomSummaryPromptTemplates"):
        save(TEMPLATES, config_id="DefaultSummaryPromptTemplates")
    assert not table.wrote_anything()


def test_malformed_json_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    table = fresh(monkeypatch)
    with pytest.raises(Exception, match="Invalid JSON"):
        save("{not json")
    assert not table.wrote_anything()


def test_a_prompt_that_is_not_text_is_rejected_at_save_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The summariser can only skip such a value, long after the save.

    Accepting it would turn a save the administrator was told had worked into a
    section missing from every later summary, with the explanation in a log the
    administrator never sees.
    """
    table = fresh(monkeypatch)
    with pytest.raises(Exception, match="must be text"):
        save({"1#SUMMARY": {"nested": "object"}})
    assert not table.wrote_anything()


def test_a_json_array_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    table = fresh(monkeypatch)
    with pytest.raises(Exception, match="must be a JSON object"):
        save([1, 2, 3])
    assert not table.wrote_anything()


def test_a_save_leaves_another_pages_settings_in_the_profile_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One record holds every page's settings, so this page writes one section.

    Nothing else is stored in the profile yet, so the record below stands for
    the first deployment where that is no longer true. Replacing the body would
    delete the other page's settings, and nothing would fail: that page would
    read the baseline and show the shipped values back.
    """
    table = fresh(monkeypatch)
    table.seed(
        CUSTOM_KEY,
        {SECTION: {"1#SUMMARY": "a prompt"}, "another_page": {"a_switch": True}},
        version=1,
    )
    save(TEMPLATES)
    stored = codec.decode_body(table.items[CUSTOM_KEY]["Body"]["B"])
    assert stored["another_page"] == {"a_switch": True}
    assert stored[SECTION] == TEMPLATES


def test_the_baseline_is_untouched_by_a_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edits are a delta; the next deployment's prompts still have to land."""
    table = fresh(monkeypatch)
    before = table.items[DEFAULT_KEY]
    save(TEMPLATES)
    assert table.items[DEFAULT_KEY] == before


# ── concurrent saves ────────────────────────────────────────────────────────


def test_a_save_against_a_superseded_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two administrators on the same page: the second must not win silently."""
    table = fresh(monkeypatch)
    save({"1#SUMMARY": "what the first admin saved"}, expected_version=1)

    with pytest.raises(Exception, match="Conflict"):
        save({"1#SUMMARY": "what the second admin saved"}, expected_version=1)

    assert table.stored_prompts() == {"1#SUMMARY": "what the first admin saved"}


def test_the_refusal_tells_the_administrator_to_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh(monkeypatch)
    save({"1#SUMMARY": "first"}, expected_version=1)
    with pytest.raises(Exception) as raised:
        save({"1#SUMMARY": "second"}, expected_version=1)
    assert "reload" in str(raised.value)


def test_the_save_is_conditional_on_the_version_the_editor_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted on the request, so the pre-check alone cannot satisfy it."""
    table = fresh(monkeypatch)
    save(TEMPLATES, expected_version=1)
    conditions = [
        request.get("ConditionExpression")
        for request in table.requests
        if request["op"] == "put_item"
    ]
    assert conditions == ["#version = :expected"]


def test_a_save_offering_no_version_is_accepted_and_still_conditional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that does not send a version keeps working, but not unguarded.

    What it loses is the check against the version *it* loaded; what it keeps is
    the condition on the record as this resolver read it, because the body it
    writes is built from that read. Asserted on the request here as well as in
    the store's own suite, since this is the only caller that passes no version.
    """
    table = fresh(monkeypatch)
    assert save(TEMPLATES, expected_version=None)["Success"] is True
    assert table.stored_prompts() == TEMPLATES

    conditional = [
        request
        for request in table.requests
        if request["op"] == "put_item" and request.get("ConditionExpression")
    ]
    assert conditional, "the save went through with no condition at all"
    assert conditional[-1]["ConditionExpression"] == "#version = :expected"


# ── group membership ────────────────────────────────────────────────────────


def test_a_member_of_the_admin_group_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh(monkeypatch)
    assert save(TEMPLATES, identity=ADMIN_IDENTITY)["Success"] is True


def test_a_single_group_claim_sent_as_a_string_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AppSync delivers cognito:groups as a bare string for a one-group caller."""
    fresh(monkeypatch)
    assert save(TEMPLATES, identity={"claims": {"cognito:groups": "Admin"}})["Success"] is True


def test_a_comma_joined_group_claim_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh(monkeypatch)
    response = save(TEMPLATES, identity={"claims": {"cognito:groups": "Users,Admin"}})
    assert response["Success"] is True


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
    identity: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse(identity, monkeypatch)


def test_the_read_is_restricted_to_the_admin_group_as_well(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field carries the same group argument, and this is the second place."""
    fresh(monkeypatch)
    with pytest.raises(Exception, match="Unauthorized"):
        read("CustomSummaryPromptTemplates", identity={"claims": {"cognito:groups": ["Users"]}})


def test_a_refusal_is_logged_without_the_caller_s_email_address(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
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
        monkeypatch,
    )

    logged = capsys.readouterr().out
    assert "someone@example.com" not in logged
    assert "11111111-2222" in logged


def test_the_written_record_keeps_the_email_address(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: the address is not dropped, only kept out of the log."""
    table = fresh(monkeypatch)
    save(TEMPLATES, identity=ADMIN_IDENTITY)

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
    identity: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent or unreadable group membership is refused, not treated as unrestricted."""
    refuse(identity, monkeypatch)


def test_group_membership_in_the_arguments_does_not_admit_a_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the identity block is consulted, so a claim in the input carries nothing."""
    table = fresh(monkeypatch)
    with pytest.raises(Exception, match="Unauthorized"):
        index.lambda_handler(
            {
                "identity": {"claims": {"cognito:groups": ["Users"]}},
                "info": {"fieldName": "updateLLMPromptTemplate"},
                "arguments": {
                    "input": {
                        "LLMPromptTemplateId": "CustomSummaryPromptTemplates",
                        "TemplateConfig": json.dumps(TEMPLATES),
                        "cognito:groups": ["Admin"],
                    }
                },
            },
            None,
        )
    assert not table.wrote_anything()


# ── audit attributes ────────────────────────────────────────────────────────


def test_the_written_record_names_the_identity_that_saved_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = fresh(monkeypatch)
    save(TEMPLATES)
    metadata = table.stored_metadata()

    assert metadata["UpdatedBy"] == "admin@example.com"
    assert metadata["UpdatedAt"].endswith("Z")


def test_the_subject_identifies_the_saver_when_no_email_claim_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = fresh(monkeypatch)
    save(
        TEMPLATES,
        identity={"claims": {"cognito:groups": ["Admin"], "sub": "11111111-2222"}},
    )

    assert table.stored_metadata()["UpdatedBy"] == "11111111-2222"


def test_audit_attributes_come_from_the_identity_not_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = fresh(monkeypatch)
    save({**TEMPLATES, "UpdatedBy": "someone-else@example.com"})

    assert table.stored_metadata()["UpdatedBy"] == "admin@example.com"
    assert "UpdatedBy" not in table.stored_prompts()


def test_a_comment_on_the_save_is_passed_to_the_revision_it_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh(monkeypatch)
    seen: Dict[str, Any] = {}

    def write_section(*_args: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return 2

    monkeypatch.setattr(index.CONFIG_STORE, "write_section", write_section)

    index.lambda_handler(
        {
            "identity": ADMIN_IDENTITY,
            "info": {"fieldName": "updateLLMPromptTemplate"},
            "arguments": {
                "input": {
                    "LLMPromptTemplateId": "CustomSummaryPromptTemplates",
                    "TemplateConfig": "{}",
                    "ExpectedVersion": 1,
                    "Comment": "why",
                }
            },
        },
        None,
    )

    assert seen["comment"] == "why"
