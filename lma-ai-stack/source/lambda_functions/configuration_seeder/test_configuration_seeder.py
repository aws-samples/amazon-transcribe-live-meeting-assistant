#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the deploy-time configuration seeder.

The failure this file exists to prevent is a stack update that resets the
summary prompt templates an administrator wrote. It is a silent one -- the
deployment succeeds, and the next meeting is summarised with the shipped
prompts -- so the whole seed sequence is run twice here and the profile's
contents are asserted afterwards, rather than only checking that a conditional
write was issued.

Also covered:

* The baseline *is* rewritten by the second run, since it is the deployment's
  own content and a new release's prompts have to land.
* An upgrade adopts the prompt templates already in the previous per-store
  table, so the move to the shared table is not experienced as a reset.
* The shipped JSON this function packages is byte-identical to the copy the
  llm-template-setup stack still ships, so the two cannot drift while both
  exist.
* A Delete leaves the table alone: emptying it would make a resource
  replacement destroy the configuration it had just written.

No AWS calls: DynamoDB is a fake and ``cfnresponse`` is replaced with a
recorder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

HERE = Path(__file__).resolve().parent
# source/lambda_functions/configuration_seeder -> source
SOURCE = HERE.parents[1]
LAYER = SOURCE / "lambda_layers" / "configuration_layer"
REPO_ROOT = HERE.parents[3]
for path in (str(HERE), str(LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

from botocore.exceptions import ClientError  # noqa: E402
from lma_config import codec  # noqa: E402
from lma_config.store import ConfigurationStore  # noqa: E402
from lma_config.test_revisions import BUCKET as HISTORY_BUCKET  # noqa: E402
from lma_config.test_revisions import FakeDynamo as HistoryDynamo  # noqa: E402
from lma_config.test_revisions import FakeS3  # noqa: E402


class FakeDynamo:
    """``get_item``/``put_item`` over a dict, with the conditions in use.

    A local copy rather than an import from the layer's own test module: the
    accessor's tests and these have different reasons to change, and a shared
    fake would couple them.
    """

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.requests: List[Dict[str, Any]] = []

    def get_item(self, **request: Any) -> Dict[str, Any]:
        self.requests.append({"op": "get_item", **request})
        key = request["Key"][codec.KEY_ATTRIBUTE]["S"]
        item = self.items.get(key)
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

    def body(self, key: str) -> Dict[str, Any]:
        return codec.decode_body(self.items[key][codec.BODY_ATTRIBUTE]["B"])

    def metadata(self, key: str) -> Dict[str, Any]:
        return codec.split_item(codec.unmarshal_item(self.items[key]))[0]


class RecordingCfnResponse:
    """Stands in for cfnresponse, keeping what was sent back."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def send(
        self, event, context, status, data, physicalResourceId=None, reason=None, **_kwargs
    ) -> None:  # noqa: N803 -- cfnresponse's own parameter name
        self.sent.append(
            {
                "status": status,
                "data": data,
                "reason": reason,
                # Defaulted the way cfnresponse itself defaults it, so a handler
                # that stops passing one shows up here as the log stream name.
                "physical_id": physicalResourceId or context.log_stream_name,
            }
        )


@pytest.fixture()
def seeder(monkeypatch: pytest.MonkeyPatch):
    """The handler module, with cfnresponse recorded rather than sent."""
    import lambda_function

    recorder = RecordingCfnResponse()
    monkeypatch.setattr(lambda_function, "cfnresponse", recorder)
    lambda_function.recorder = recorder
    return lambda_function


def build_store() -> tuple[ConfigurationStore, FakeDynamo]:
    dynamo = FakeDynamo()
    return ConfigurationStore("lma-configuration", client=dynamo), dynamo


# ── the shipped content ────────────────────────────────────────────────────


def test_the_shipped_prompts_are_numbered_in_the_order_they_appear(seeder) -> None:
    """N orders the sections of the summary, and the reader selects on that form."""
    assert seeder.numbered_prompts({"SUMMARY": "a", "DETAILS": "b", "ACTIONS": "c"}) == {
        "1#SUMMARY": "a",
        "2#DETAILS": "b",
        "3#ACTIONS": "c",
    }


def test_the_baseline_document_holds_the_shipped_prompts_under_its_section(seeder) -> None:
    body = seeder.shipped_default_body("default")
    assert set(body) == {
        "summary_prompt_templates",
        "nova_sonic_config",
        "chat_buttons",
        "call_pricing",
        "asr_engine",
        # Filled from the stack parameters by `seed`; see the tests under
        # "the stack parameters that are defaults".
        "language_models",
        "transcription",
        "recording",
        "meeting_assist",
        "meeting_processing",
        "virtual_participant",
    }
    assert body["language_models"] == {}
    assert set(body["summary_prompt_templates"]) == {
        "1#SUMMARY",
        "2#DETAILS",
        "3#ACTIONS",
        "4#AGENDA COVERAGE",
        "5#SLIDES PRESENTED",
        "6#CHAPTERS",
    }
    assert "{transcript}" in body["summary_prompt_templates"]["1#SUMMARY"]


def test_the_baseline_carries_a_dated_price_table_in_one_currency(seeder) -> None:
    """An estimate states the date and the source of its prices; both must be there."""
    pricing = seeder.shipped_default_body("default")["call_pricing"]
    assert pricing["currency"] == "USD"
    assert pricing["pricesAsOf"] and pricing["source"]
    assert pricing["baseRegion"] in pricing["pricedRegions"]
    base = pricing["regions"]["default"]
    for service, units in base.items():
        assert "/" in service, f"{service} is not '<service>/<api-or-model>'"
        for unit, price in units.items():
            assert isinstance(price, (int, float)) and price >= 0, f"{service} {unit}: {price!r}"


def test_every_model_a_deployment_can_choose_has_a_price(seeder) -> None:
    """A model offered in lma-main.yaml with no price shows as unpriced on every meeting."""
    import re

    main = (Path(__file__).resolve().parents[4] / "lma-main.yaml").read_text(encoding="utf-8")
    offered = set(
        re.findall(r'"((?:global|us)\.(?:amazon|anthropic)\.[^"]+|amazon\.nova-[^"]+)"', main)
    )
    assert offered, "found no model ids in lma-main.yaml, so this test checks nothing"
    base = seeder.shipped_default_body("default")["call_pricing"]["regions"]["default"]
    missing = sorted(m for m in offered if f"bedrock/{m}" not in base)
    assert not missing, f"no price for: {missing}"
    for model_id in offered:
        units = base[f"bedrock/{model_id}"]
        assert units["inputTokens"] > 0 and units["outputTokens"] > 0


def test_the_healthcare_domain_ships_its_own_prompts(seeder) -> None:
    body = seeder.shipped_default_body("healthcare")
    assert "4#BIRP" in body["summary_prompt_templates"]


def test_an_unrecognised_domain_falls_back_to_the_default_prompts(seeder) -> None:
    """A domain nobody shipped content for must not deploy an empty baseline."""
    assert seeder.shipped_default_body("veterinary") == seeder.shipped_default_body("default")


@pytest.mark.parametrize(
    "filename", ["LLMPromptSummaryTemplate.json", "LLMPromptHealthcareSummaryTemplate.json"]
)
def test_the_shipped_prompts_match_the_copy_the_other_stack_still_ships(filename: str) -> None:
    """Two stacks ship these until the per-store table is retired.

    Both copies exist on purpose -- the llm-template-setup stack keeps writing
    its own table so a deployment can be rolled back -- but they must be the
    same prompts, and nothing else would notice if they stopped being.

    Compared as ordered pairs, not as dicts: both this seeder's
    `numbered_prompts` and the other stack's `get_new_item` number the sections
    by the order the keys appear in, so two copies holding the same prompts in a
    different order would number them differently. The baseline would then
    order the summary's sections differently from the table a rollback reads,
    and a dict comparison would have passed.
    """
    mine = (HERE / "defaults" / filename).read_text(encoding="utf-8")
    theirs = (
        REPO_ROOT / "lma-llm-template-setup-stack" / "source" / "lambda_functions" / filename
    ).read_text(encoding="utf-8")
    assert list(json.loads(mine).items()) == list(json.loads(theirs).items())


def _nova_sonic_stack_seeder(monkeypatch: pytest.MonkeyPatch):
    """The nova-sonic-config stack's own seeder module, loaded by path.

    Its directory holds a `cfnresponse` of its own, so it is loaded under a
    private name with a placeholder in `sys.modules` rather than by putting that
    directory on the path, where its `cfnresponse` would shadow this one's.
    """
    import importlib.util
    import types

    monkeypatch.setitem(sys.modules, "cfnresponse", types.ModuleType("cfnresponse"))
    path = (
        REPO_ROOT
        / "lma-nova-sonic-config-stack"
        / "source"
        / "lambda_functions"
        / "nova_sonic_config_upload.py"
    )
    spec = importlib.util.spec_from_file_location("nova_sonic_config_upload_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shipped_nova_sonic_defaults_match_the_copy_the_other_stack_still_ships(
    seeder, monkeypatch
) -> None:
    """The same arrangement as the prompts: two copies until the old table retires.

    The nova-sonic-config stack keeps seeding its own table so a deployment can
    be rolled back to a release that reads it. Only the configuration values are
    compared; the partition key, the guidance text and the description are that
    table's own bookkeeping.
    """
    theirs = _nova_sonic_stack_seeder(monkeypatch).get_default_config()
    for bookkeeping in ("NovaSonicConfigId", "*Information*", "description"):
        theirs.pop(bookkeeping)
    assert seeder.shipped_default_body("default")["nova_sonic_config"] == theirs


def test_the_nova_sonic_defaults_hold_only_fields_the_page_can_set(seeder) -> None:
    """A shipped field the page cannot override would be one no admin could change."""
    shipped = seeder.shipped_default_body("default")["nova_sonic_config"]
    assert set(shipped) == seeder.NOVA_SONIC_FIELDS


def test_the_nova_sonic_defaults_are_the_same_for_every_domain(seeder) -> None:
    """Only the summary prompts have a healthcare variant."""
    assert (
        seeder.shipped_default_body("healthcare")["nova_sonic_config"]
        == seeder.shipped_default_body("default")["nova_sonic_config"]
    )


# ── carrying a later store's edits across, one section at a time ───────────


class LegacyNovaSonicTable:
    """The previous Nova Sonic table, keyed on its own partition-key attribute."""

    def __init__(self, item: Dict[str, Any] | None = None) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        if item is not None:
            self.items["CustomNovaSonicConfig"] = item
        self.reads = 0

    def get_item(self, **request: Any) -> Dict[str, Any]:
        self.reads += 1
        key = request["Key"]["NovaSonicConfigId"]["S"]
        item = self.items.get(key)
        return {"Item": item} if item is not None else {}


CUSTOMISED_NOVA_SONIC_ITEM = {
    "NovaSonicConfigId": {"S": "CustomNovaSonicConfig"},
    "*Information*": {"S": "guidance text"},
    "description": {"S": "Custom Nova Sonic configuration."},
    "systemPrompt": {"S": "You are Sam, the team's assistant."},
    "promptMode": {"S": "inject"},
    "meetingMode": {"S": "translator"},
    "translatorLanguageA": {"S": "French"},
    "translatorLanguageB": {"S": "German"},
    "groupMeetingMode": {"BOOL": False},
    "UpdatedBy": {"S": "admin@example.com"},
    "UpdatedAt": {"S": "2026-10-01T00:00:00Z"},
}

NOVA_TABLES = {"nova_sonic_config": "old-nova-table"}


def adoption(outcome: Dict[str, str]) -> Dict[str, str]:
    """The SectionAdoption report, as section -> outcome."""
    return dict(pair.split("=", 1) for pair in outcome["SectionAdoption"].split(","))


def test_an_upgrade_carries_the_nova_sonic_settings_into_the_existing_profile(seeder) -> None:
    """The upgrade an existing deployment makes: the profile is already there."""
    store, dynamo = build_store()
    seeder.seed(store, domain="default")  # the release that created the profile
    store.write_section(
        "custom",
        "summary_prompt_templates",
        {"1#SUMMARY": "an admin's prompt"},
        updated_by="admin@example.com",
        expected_version=None,
    )

    outcome = seeder.seed(
        store,
        domain="default",
        legacy_section_tables=NOVA_TABLES,
        legacy_client=LegacyNovaSonicTable(CUSTOMISED_NOVA_SONIC_ITEM),
    )

    assert adoption(outcome)["nova_sonic_config"] == "adopted-from-previous-table"
    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"1#SUMMARY": "an admin's prompt"},
        "nova_sonic_config": {
            "systemPrompt": "You are Sam, the team's assistant.",
            "promptMode": "inject",
            "meetingMode": "translator",
            "translatorLanguageA": "French",
            "translatorLanguageB": "German",
            "groupMeetingMode": False,
        },
    }
    resolved = store.get_section("nova_sonic_config").values
    assert resolved["translatorLanguageA"] == "French"
    assert resolved["voiceId"] == "tiffany"  # from the baseline


def test_a_fresh_deployment_records_an_empty_nova_sonic_section(seeder) -> None:
    """The previous table holds only its guidance text on a new deployment."""
    store, dynamo = build_store()
    legacy = LegacyNovaSonicTable(
        {
            "NovaSonicConfigId": {"S": "CustomNovaSonicConfig"},
            "*Information*": {"S": "guidance text"},
            "description": {"S": "Custom Nova Sonic configuration."},
        }
    )
    outcome = seeder.seed(
        store, domain="default", legacy_section_tables=NOVA_TABLES, legacy_client=legacy
    )
    assert outcome["Profile"] == "created"
    assert adoption(outcome)["nova_sonic_config"] == "nothing-to-adopt"
    assert dynamo.body("Config#custom") == {"nova_sonic_config": {}}


def test_the_nova_sonic_adoption_happens_once(seeder) -> None:
    """A later deployment must not replace an edit with the pre-upgrade values."""
    store, dynamo = build_store()
    legacy = LegacyNovaSonicTable(CUSTOMISED_NOVA_SONIC_ITEM)
    seeder.seed(store, domain="default", legacy_section_tables=NOVA_TABLES, legacy_client=legacy)
    store.write_section(
        "custom",
        "nova_sonic_config",
        {"translatorLanguageA": "Japanese"},
        updated_by="admin@example.com",
        expected_version=None,
    )
    reads_before = legacy.reads

    outcome = seeder.seed(
        store, domain="default", legacy_section_tables=NOVA_TABLES, legacy_client=legacy
    )

    assert adoption(outcome)["nova_sonic_config"] == "already-present"
    assert legacy.reads == reads_before, "the previous table was read again"
    assert dynamo.body("Config#custom")["nova_sonic_config"] == {"translatorLanguageA": "Japanese"}


def test_a_reset_to_defaults_is_not_undone_by_the_next_deployment(seeder) -> None:
    """Reset saves an empty section, and an empty section is present."""
    store, dynamo = build_store()
    legacy = LegacyNovaSonicTable(CUSTOMISED_NOVA_SONIC_ITEM)
    seeder.seed(store, domain="default", legacy_section_tables=NOVA_TABLES, legacy_client=legacy)
    store.write_section(
        "custom", "nova_sonic_config", {}, updated_by="admin@example.com", expected_version=None
    )
    seeder.seed(store, domain="default", legacy_section_tables=NOVA_TABLES, legacy_client=legacy)
    assert dynamo.body("Config#custom")["nova_sonic_config"] == {}


def test_an_unreadable_previous_nova_sonic_table_is_retried_on_the_next_run(seeder) -> None:
    """Unlike the profile's adoption, this one is not a one-shot.

    Nothing is written when the read fails, so the section stays absent and the
    next deployment that re-seeds reads the previous table again.
    """
    store, dynamo = build_store()
    first = seeder.seed(
        store, domain="default", legacy_section_tables=NOVA_TABLES, legacy_client=Unreadable()
    )
    assert adoption(first)["nova_sonic_config"] == "previous-table-unreadable"
    assert "nova_sonic_config" not in dynamo.body("Config#custom")

    later = seeder.seed(
        store,
        domain="default",
        legacy_section_tables=NOVA_TABLES,
        legacy_client=LegacyNovaSonicTable(CUSTOMISED_NOVA_SONIC_ITEM),
    )
    assert adoption(later)["nova_sonic_config"] == "adopted-from-previous-table"
    assert dynamo.body("Config#custom")["nova_sonic_config"]["translatorLanguageB"] == "German"


def test_an_unreadable_nova_sonic_table_does_not_stop_an_admins_save_counting(seeder) -> None:
    """Once an admin saves the section, a later successful read changes nothing."""
    store, dynamo = build_store()
    seeder.seed(
        store, domain="default", legacy_section_tables=NOVA_TABLES, legacy_client=Unreadable()
    )
    store.write_section(
        "custom",
        "nova_sonic_config",
        {"voiceId": "matthew"},
        updated_by="admin@example.com",
        expected_version=None,
    )
    later = seeder.seed(
        store,
        domain="default",
        legacy_section_tables=NOVA_TABLES,
        legacy_client=LegacyNovaSonicTable(CUSTOMISED_NOVA_SONIC_ITEM),
    )
    assert adoption(later)["nova_sonic_config"] == "already-present"
    assert dynamo.body("Config#custom")["nova_sonic_config"] == {"voiceId": "matthew"}


def test_no_previous_nova_sonic_table_writes_nothing(seeder) -> None:
    """A stack given no previous table has nothing to record either way."""
    store, dynamo = build_store()
    outcome = seeder.seed(store, domain="default")
    assert adoption(outcome)["nova_sonic_config"] == "no-previous-table"
    assert dynamo.body("Config#custom") == {}


def test_a_save_landing_mid_adoption_is_kept_and_the_adoption_retried(seeder, monkeypatch) -> None:
    """An administrator saving another page during the deployment loses nothing."""
    store, dynamo = build_store()
    seeder.seed(store, domain="default")
    real_seed_section = store.seed_section
    raced = {"done": False}

    def seed_section_after_a_concurrent_save(name, section, values):
        if not raced["done"]:
            raced["done"] = True
            store.write_section(
                "custom",
                "summary_prompt_templates",
                {"1#SUMMARY": "saved during the deployment"},
                updated_by="admin@example.com",
                expected_version=None,
            )
            # The profile moved on since the adoption read it.
            raise seeder.ConcurrentModification("changed while it was being saved")
        return real_seed_section(name, section, values)

    monkeypatch.setattr(store, "seed_section", seed_section_after_a_concurrent_save)
    outcome = seeder.seed(
        store,
        domain="default",
        legacy_section_tables=NOVA_TABLES,
        legacy_client=LegacyNovaSonicTable(CUSTOMISED_NOVA_SONIC_ITEM),
    )
    assert adoption(outcome)["nova_sonic_config"] == "adopted-from-previous-table"
    body = dynamo.body("Config#custom")
    assert body["summary_prompt_templates"] == {"1#SUMMARY": "saved during the deployment"}
    assert body["nova_sonic_config"]["meetingMode"] == "translator"


def test_the_handler_passes_the_previous_nova_sonic_table_through(seeder, monkeypatch) -> None:
    dynamo = FakeDynamo()
    monkeypatch.setattr(
        seeder, "ConfigurationStore", lambda name, **_: ConfigurationStore(name, client=dynamo)
    )
    seen: Dict[str, Any] = {}
    real_seed = seeder.seed

    def recording_seed(store, **kwargs):
        seen.update(kwargs)
        return real_seed(store, domain=kwargs["domain"])

    monkeypatch.setattr(seeder, "seed", recording_seed)
    event = _event("Update")
    event["ResourceProperties"]["LegacyNovaSonicConfigTableName"] = "old-nova-table"
    seeder.handler(event, _Context())
    assert seen["legacy_section_tables"] == {
        "nova_sonic_config": "old-nova-table",
        "chat_buttons": "",
        "asr_engine": "",
    }
    assert "SectionAdoption" in seeder.recorder.sent[-1]["data"]


# ── the chat buttons ───────────────────────────────────────────────────────


def _chat_button_stack_seeder(monkeypatch: pytest.MonkeyPatch):
    """The chat-button-config stack's own seeder, loaded by path (see above)."""
    import importlib.util
    import types

    monkeypatch.setitem(sys.modules, "cfnresponse", types.ModuleType("cfnresponse"))
    path = (
        REPO_ROOT
        / "lma-chat-button-config-stack"
        / "source"
        / "lambda_functions"
        / "chat_button_upload.py"
    )
    spec = importlib.util.spec_from_file_location("chat_button_upload_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shipped_chat_buttons_match_the_copy_the_other_stack_still_ships() -> None:
    """Compared as ordered pairs: the order is the buttons' sequence numbers."""
    mine = (HERE / "defaults" / "ChatButtonConfig.json").read_text(encoding="utf-8")
    theirs = (
        REPO_ROOT
        / "lma-chat-button-config-stack"
        / "source"
        / "lambda_functions"
        / "ChatButtonConfig.json"
    ).read_text(encoding="utf-8")
    assert list(json.loads(mine).items()) == list(json.loads(theirs).items())


def test_the_shipped_chat_buttons_are_numbered_as_the_other_stack_numbers_them(
    seeder, monkeypatch
) -> None:
    """The same keys, so a rollback's table and this baseline name the same buttons."""
    shipped = json.loads((HERE / "defaults" / "ChatButtonConfig.json").read_text(encoding="utf-8"))
    theirs = _chat_button_stack_seeder(monkeypatch).get_new_item("Default", "info", shipped)
    for bookkeeping in ("ChatButtonConfigId", "*Information*"):
        theirs.pop(bookkeeping)
    assert seeder.shipped_default_body("default")["chat_buttons"] == theirs


CUSTOMISED_CHAT_BUTTON_ITEM = {
    "ChatButtonConfigId": {"S": "CustomChatButtonConfig"},
    "*Information*": {"S": "guidance text"},
    "1#MY SUMMARY": {"S": "Summarize in three bullets"},
    "2#RISKS": {"S": "What risks were raised?"},
    "UpdatedBy": {"S": "admin@example.com"},
    "UpdatedAt": {"S": "2026-10-01T00:00:00Z"},
}


class LegacyChatButtonTable:
    """The previous chat-button table, keyed on its own partition-key attribute."""

    def __init__(self, item: Dict[str, Any] | None = None) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        if item is not None:
            self.items["CustomChatButtonConfig"] = item

    def get_item(self, **request: Any) -> Dict[str, Any]:
        item = self.items.get(request["Key"]["ChatButtonConfigId"]["S"])
        return {"Item": item} if item is not None else {}


CHAT_TABLES = {"chat_buttons": "old-chat-table"}


def test_an_upgrade_carries_the_custom_chat_buttons_across_whole(seeder) -> None:
    """A custom list replaces the shipped one, so it is carried as the list it is."""
    store, dynamo = build_store()
    seeder.seed(store, domain="default")

    outcome = seeder.seed(
        store,
        domain="default",
        legacy_section_tables=CHAT_TABLES,
        legacy_client=LegacyChatButtonTable(CUSTOMISED_CHAT_BUTTON_ITEM),
    )

    assert adoption(outcome)["chat_buttons"] == "adopted-from-previous-table"
    assert dynamo.body("Config#custom")["chat_buttons"] == {
        "1#MY SUMMARY": "Summarize in three bullets",
        "2#RISKS": "What risks were raised?",
    }


def test_a_custom_button_list_identical_to_the_shipped_one_is_still_carried(seeder) -> None:
    """Nothing is pruned: dropping a button would change what the list replaces."""
    store, dynamo = build_store()
    shipped = seeder.shipped_default_body("default")["chat_buttons"]
    item = {"ChatButtonConfigId": {"S": "CustomChatButtonConfig"}}
    item.update({key: {"S": value} for key, value in shipped.items()})

    seeder.seed(
        store,
        domain="default",
        legacy_section_tables=CHAT_TABLES,
        legacy_client=LegacyChatButtonTable(item),
    )

    assert dynamo.body("Config#custom")["chat_buttons"] == shipped


def test_a_fresh_deployment_records_an_empty_chat_button_section(seeder) -> None:
    """The previous table holds only its guidance text on a new deployment."""
    store, dynamo = build_store()
    outcome = seeder.seed(
        store,
        domain="default",
        legacy_section_tables=CHAT_TABLES,
        legacy_client=LegacyChatButtonTable(
            {
                "ChatButtonConfigId": {"S": "CustomChatButtonConfig"},
                "*Information*": {"S": "guidance text"},
            }
        ),
    )
    assert adoption(outcome)["chat_buttons"] == "nothing-to-adopt"
    assert dynamo.body("Config#custom") == {"chat_buttons": {}}


def test_both_later_stores_are_carried_in_one_run(seeder) -> None:
    """Each section is read from its own table, through the one client."""

    class BothTables:
        def __init__(self) -> None:
            self.nova = LegacyNovaSonicTable(CUSTOMISED_NOVA_SONIC_ITEM)
            self.chat = LegacyChatButtonTable(CUSTOMISED_CHAT_BUTTON_ITEM)

        def get_item(self, **request: Any) -> Dict[str, Any]:
            table = {"old-nova-table": self.nova, "old-chat-table": self.chat}[request["TableName"]]
            return table.get_item(**request)

    store, dynamo = build_store()
    outcome = seeder.seed(
        store,
        domain="default",
        legacy_section_tables={**NOVA_TABLES, **CHAT_TABLES},
        legacy_client=BothTables(),
    )

    assert adoption(outcome) == {
        "nova_sonic_config": "adopted-from-previous-table",
        "chat_buttons": "adopted-from-previous-table",
        "asr_engine": "no-previous-table",
    }
    body = dynamo.body("Config#custom")
    assert body["chat_buttons"]["2#RISKS"] == "What risks were raised?"
    assert body["nova_sonic_config"]["translatorLanguageA"] == "French"


# ── the ASR engine switches ────────────────────────────────────────────────


class LegacyAsrConfigTable:
    """The previous ASR config table, keyed on its own partition-key attribute."""

    def __init__(self, item: Dict[str, Any] | None = None) -> None:
        self.item = item

    def get_item(self, **request: Any) -> Dict[str, Any]:
        assert request["Key"] == {"AsrConfigId": {"S": "CustomAsrConfig"}}
        return {"Item": self.item} if self.item is not None else {}


ASR_TABLES = {"asr_engine": "old-asr-table"}


def test_the_shipped_asr_switches_are_all_off(seeder) -> None:
    """Every meeting is transcribed by Amazon Transcribe until an admin chooses otherwise."""
    assert seeder.shipped_default_body("default")["asr_engine"] == {
        "streamingEngineMicrovm": False,
        "virtualParticipantEngineMicrovm": False,
        "diarizeVirtualParticipant": False,
    }


def test_an_upgrade_carries_the_asr_switches_across(seeder) -> None:
    store, dynamo = build_store()
    seeder.seed(store, domain="default")

    outcome = seeder.seed(
        store,
        domain="default",
        legacy_section_tables=ASR_TABLES,
        legacy_client=LegacyAsrConfigTable(
            {
                "AsrConfigId": {"S": "CustomAsrConfig"},
                "streamingEngineMicrovm": {"BOOL": True},
                "diarizeVirtualParticipant": {"BOOL": True},
                # Retired by an earlier release; nothing reads it.
                "speakerThreshold": {"S": "0.35"},
                "UpdatedBy": {"S": "admin@example.com"},
            }
        ),
    )

    assert adoption(outcome)["asr_engine"] == "adopted-from-previous-table"
    assert dynamo.body("Config#custom")["asr_engine"] == {
        "streamingEngineMicrovm": True,
        "diarizeVirtualParticipant": True,
    }
    resolved = store.get_section("asr_engine").values
    assert resolved["virtualParticipantEngineMicrovm"] is False  # from the baseline


def test_an_asr_table_that_was_never_written_records_an_empty_section(seeder) -> None:
    """The previous table had no seeder, so an untouched deployment has no record."""
    store, dynamo = build_store()
    outcome = seeder.seed(
        store,
        domain="default",
        legacy_section_tables=ASR_TABLES,
        legacy_client=LegacyAsrConfigTable(None),
    )
    assert adoption(outcome)["asr_engine"] == "nothing-to-adopt"
    assert dynamo.body("Config#custom") == {"asr_engine": {}}


def test_a_deployment_without_the_asr_engine_records_nothing(seeder) -> None:
    """No table is passed, so enabling the engine later still carries its record."""
    store, dynamo = build_store()
    outcome = seeder.seed(store, domain="default", legacy_section_tables={"asr_engine": ""})
    assert adoption(outcome)["asr_engine"] == "no-previous-table"
    assert "asr_engine" not in dynamo.body("Config#custom")


# ── a stack update must not reset an operator's edits ──────────────────────


def test_the_origin_of_a_profile_outlives_the_run_that_created_it(seeder) -> None:
    """The one outcome somebody must act on has to survive every later run.

    The content hash makes the seeder run again on any release that changes
    either of its directories, and every one of those finds the profile present.
    If the reported value were only what *this* run did, an operator checking a
    deployment or two later would read "kept" and never learn that the prompts
    they had customised were never carried across.
    """
    store, dynamo = build_store()
    first = seeder.seed(
        store, domain="default", legacy_table_name="old-table", legacy_client=Unreadable()
    )
    assert first["ProfileOrigin"] == "created-previous-table-unreadable"

    # Two later deployments, and an administrator's save in between -- which is
    # a whole-item write, so this also pins that the mark is carried forward.
    assert seeder.seed(store, domain="default")["ProfileOrigin"] == (
        "created-previous-table-unreadable"
    )
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "applied again by hand"}},
        updated_by="admin@example.com",
        expected_version=1,
    )
    later = seeder.seed(store, domain="default")
    assert later["Profile"] == "kept"
    assert later["ProfileOrigin"] == "created-previous-table-unreadable"
    assert dynamo.metadata("Config#custom")["ProfileOrigin"] == (
        "created-previous-table-unreadable"
    )


def test_a_profile_carrying_no_origin_is_reported_as_unknown(seeder) -> None:
    """Only a record created outside the seeder can be in that state."""
    store, dynamo = build_store()
    store.write_default(seeder.shipped_default_body("default"))
    store.write_profile("custom", {}, updated_by="admin@example.com", expected_version=0)
    assert seeder.seed(store, domain="default")["ProfileOrigin"] == "unknown"
    assert "ProfileOrigin" not in dynamo.metadata("Config#custom")


def test_a_second_deployment_keeps_the_prompts_an_administrator_wrote(seeder) -> None:
    """The whole reason this function writes the profile conditionally."""
    store, dynamo = build_store()
    seeder.seed(store, domain="default")
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "the prompt an admin wrote"}},
        updated_by="admin@example.com",
        expected_version=1,
    )

    outcome = seeder.seed(store, domain="default")

    assert outcome["Profile"] == "kept"
    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"1#SUMMARY": "the prompt an admin wrote"}
    }
    assert store.get_section("summary_prompt_templates").values["1#SUMMARY"] == (
        "the prompt an admin wrote"
    )


def test_the_profile_write_is_conditional_as_well_as_guarded_by_a_read(seeder) -> None:
    """Two things protect an operator's edits, and this pins the second.

    The seeder reads the record first and skips the write when one is there,
    which is what makes the outcome legible in the log. But a read followed by a
    write is two operations, so the write itself is conditional on the record
    being absent -- asserted on the request, because the read-and-skip above
    would make a test of the outcome alone pass either way.
    """
    store, dynamo = build_store()
    seeder.seed(store, domain="default")
    conditions = [
        request.get("ConditionExpression")
        for request in dynamo.requests
        if request["op"] == "put_item" and request["Item"]["Configuration"]["S"] == "Config#custom"
    ]
    assert conditions == ["attribute_not_exists(#key)"]


def test_a_second_deployment_does_update_the_baseline(seeder) -> None:
    """A new release's prompts have to reach the table."""
    store, dynamo = build_store()
    seeder.seed(store, domain="default")
    seeder.seed(store, domain="healthcare")
    assert "4#BIRP" in dynamo.body("Config#default")["summary_prompt_templates"]


def test_the_baseline_is_marked_as_one_a_deployment_owns(seeder) -> None:
    """So a UI can show it read-only instead of offering an edit that is lost."""
    store, dynamo = build_store()
    seeder.seed(store, domain="default")
    metadata = dynamo.metadata("Config#default")
    assert metadata["Managed"] is True
    assert "deployment" in metadata["Description"].lower()


def test_the_active_pointer_is_set_once_and_then_left_alone(seeder) -> None:
    store, dynamo = build_store()
    assert seeder.seed(store, domain="default")["ActiveProfile"] == "created"
    store.set_active_profile("production")
    assert seeder.seed(store, domain="default")["ActiveProfile"] == "kept"
    assert dynamo.metadata("Config#__active")["ActiveProfile"] == "production"


def test_a_profile_deleted_by_hand_is_recreated_on_the_next_deployment(seeder) -> None:
    """Why the condition is on the record, not on the CloudFormation request type.

    Keying off `RequestType == 'Create'` would leave a deployment whose profile
    record is missing -- restored table, hand-deleted item -- with no profile at
    all until the stack was recreated.
    """
    store, dynamo = build_store()
    seeder.seed(store, domain="default")
    del dynamo.items["Config#custom"]
    assert seeder.seed(store, domain="default")["Profile"] == "created"
    assert "Config#custom" in dynamo.items


# ── adopting the previous table ────────────────────────────────────────────


def test_an_upgrade_adopts_the_prompts_already_in_the_previous_table(seeder) -> None:
    """The move to the shared table must not read as a reset to the operator."""
    legacy = FakeDynamo()
    legacy.items["CustomSummaryPromptTemplates"] = {
        "LLMPromptTemplateId": {"S": "CustomSummaryPromptTemplates"},
        "1#SUMMARY": {"S": "a prompt written before the upgrade"},
        "*Information*": {"S": "guidance text"},
        "UpdatedBy": {"S": "admin@example.com"},
    }
    legacy.get_item = _legacy_get_item(legacy)  # type: ignore[method-assign]

    store, dynamo = build_store()
    outcome = seeder.seed(
        store, domain="default", legacy_table_name="old-table", legacy_client=legacy
    )

    assert outcome["Profile"] == "created"
    assert outcome["ProfileOrigin"] == "created-from-previous-table"
    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"1#SUMMARY": "a prompt written before the upgrade"}
    }
    assert store.get_section("summary_prompt_templates").values["1#SUMMARY"] == (
        "a prompt written before the upgrade"
    )


def test_adoption_lines_a_prompt_up_with_the_baseline_by_its_title(seeder) -> None:
    """The page that wrote the old records numbered sections by their position.

    So a deployment that had removed or reordered a shipped section stored the
    rest under numbers that no longer line up with the release's -- `1#DETAILS`
    where the baseline has `2#DETAILS`. Adopted verbatim, that is an addition
    rather than an override, and the merged document would hold the section
    twice.
    """
    legacy = FakeDynamo()
    legacy.items["CustomSummaryPromptTemplates"] = {
        "LLMPromptTemplateId": {"S": "CustomSummaryPromptTemplates"},
        # What the old page stored for a deployment that deleted SUMMARY.
        "1#DETAILS": {"S": "my details prompt"},
        "2#ACTIONS": {"S": "my actions prompt"},
    }
    legacy.get_item = _legacy_get_item(legacy)  # type: ignore[method-assign]

    store, dynamo = build_store()
    seeder.seed(store, domain="default", legacy_table_name="old-table", legacy_client=legacy)

    # Stored under the baseline's numbers, so the profile is a delta.
    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {
            "2#DETAILS": "my details prompt",
            "3#ACTIONS": "my actions prompt",
        }
    }
    # And the editor and the summariser see the shipped sections once each,
    # not the two adopted ones a second time.
    merged = store.get_section("summary_prompt_templates").values
    assert sorted(merged) == [
        "1#SUMMARY",
        "2#DETAILS",
        "3#ACTIONS",
        "4#AGENDA COVERAGE",
        "5#SLIDES PRESENTED",
        "6#CHAPTERS",
    ]
    assert merged["2#DETAILS"] == "my details prompt"
    assert merged["3#ACTIONS"] == "my actions prompt"


def test_adoption_drops_a_prompt_the_release_already_ships(seeder) -> None:
    """The old page saved the whole list, not only what was edited.

    So a deployment whose administrator ever pressed Save arrives with every
    section stored, including the ones they never touched. Kept, those pin the
    prompts of the release they were saved under, and no later release's
    improvement to any of them would reach that deployment again -- which is
    exactly what the profile being a delta is supposed to prevent. The merged
    document is identical either way; what differs is what happens at the next
    upgrade.
    """
    legacy = FakeDynamo()
    baseline = seeder.shipped_default_body("default")["summary_prompt_templates"]
    shipped_summary = baseline["1#SUMMARY"]
    legacy.items["CustomSummaryPromptTemplates"] = {
        "LLMPromptTemplateId": {"S": "CustomSummaryPromptTemplates"},
        # Untouched: byte-identical to what this release ships.
        "1#SUMMARY": {"S": shipped_summary},
        # Edited.
        "2#DETAILS": {"S": "my own details prompt"},
    }
    legacy.get_item = _legacy_get_item(legacy)  # type: ignore[method-assign]

    store, dynamo = build_store()
    seeder.seed(store, domain="default", legacy_table_name="old-table", legacy_client=legacy)

    assert dynamo.body("Config#custom") == {
        "summary_prompt_templates": {"2#DETAILS": "my own details prompt"}
    }
    # And the summariser still sees both, one inherited and one overridden.
    merged = store.get_section("summary_prompt_templates").values
    assert merged["1#SUMMARY"] == shipped_summary
    assert merged["2#DETAILS"] == "my own details prompt"


def test_a_record_holding_only_shipped_prompts_is_reported_as_nothing_adopted(
    seeder,
) -> None:
    """The outcome has to say what happened, because somebody may act on it.

    The old page saved the whole list without editing anything, so this is the
    ordinary shape for a deployment that never customised a prompt. Every one of
    them is dropped, nothing is carried across, and reporting
    `created-from-previous-table` would tell an operator the opposite of the
    truth -- in an output that exists precisely so they can act on it.
    """
    legacy = FakeDynamo()
    baseline = seeder.shipped_default_body("default")["summary_prompt_templates"]
    legacy.items["CustomSummaryPromptTemplates"] = {
        "LLMPromptTemplateId": {"S": "CustomSummaryPromptTemplates"},
        **{key: {"S": value} for key, value in baseline.items()},
    }
    legacy.get_item = _legacy_get_item(legacy)  # type: ignore[method-assign]

    store, dynamo = build_store()
    outcome = seeder.seed(
        store, domain="default", legacy_table_name="old-table", legacy_client=legacy
    )
    assert outcome["Profile"] == "created"
    assert outcome["ProfileOrigin"] == "created"
    assert dynamo.body("Config#custom") == {}
    assert seeder.adopt_legacy_prompts("old-table", baseline, client=legacy) == {}


def test_adoption_keeps_a_prompt_that_differs_from_the_one_now_shipped(seeder) -> None:
    """Even though a release may have changed that section since it was saved.

    "Saved without editing, under an older release" and "deliberately pasted the
    text that release shipped" are indistinguishable from here, so text an
    operator may have written is kept. The next save from the page prunes it if
    they did not want it.
    """
    assert seeder.align_with_baseline(
        {"1#SUMMARY": "the previous release's wording"},
        {"1#SUMMARY": "this release's wording"},
    ) == {"1#SUMMARY": "the previous release's wording"}


def test_adoption_keeps_the_number_of_a_section_the_release_does_not_ship(seeder) -> None:
    """There is nothing to line it up with, so it keeps its own key."""
    assert seeder.align_with_baseline(
        {"4#RISKS": "list the risks"}, {"1#SUMMARY": "a", "2#DETAILS": "b"}
    ) == {"4#RISKS": "list the risks"}


def test_adoption_against_no_baseline_leaves_the_prompts_alone(seeder) -> None:
    assert seeder.align_with_baseline({"1#DETAILS": "d"}, {}) == {"1#DETAILS": "d"}


def test_two_adopted_prompts_with_one_title_keep_the_first_and_say_so(seeder, caplog) -> None:
    """Both cannot line up with the single key the release ships.

    The old page's positional renumbering could leave one title under two
    numbers. Dropping one is unavoidable; dropping it silently is not, because
    the operator whose prompt it was is the only person who can tell which of
    the two they wanted. Which one survives is the lower key rather than
    whichever the dict happened to yield first.
    """
    with caplog.at_level("WARNING"):
        aligned = seeder.align_with_baseline(
            {"3#DETAILS": "the later one", "1#DETAILS": "the earlier one"},
            {"2#DETAILS": "shipped"},
        )
    assert aligned == {"2#DETAILS": "the earlier one"}
    assert "3#DETAILS" in caplog.text
    assert "Transcript Summary" in caplog.text


def test_adoption_takes_only_the_prompt_attributes(seeder) -> None:
    """The guidance text and the audit attributes are not prompts."""
    legacy = FakeDynamo()
    legacy.items["CustomSummaryPromptTemplates"] = {
        "LLMPromptTemplateId": {"S": "CustomSummaryPromptTemplates"},
        "1#SUMMARY": {"S": "a prompt"},
        "*Information*": {"S": "guidance text"},
        "UpdatedAt": {"S": "2026-01-01T00:00:00Z"},
        "NotANumber#Section": {"S": "a hand-added attribute"},
    }
    legacy.get_item = _legacy_get_item(legacy)  # type: ignore[method-assign]
    adopted = seeder.adopt_legacy_prompts("old-table", client=legacy)
    assert adopted == {"summary_prompt_templates": {"1#SUMMARY": "a prompt"}}


def test_adoption_of_an_empty_previous_record_leaves_an_empty_profile(seeder) -> None:
    """The ordinary case: a new deployment, or one that never customised."""
    legacy = FakeDynamo()
    legacy.items["CustomSummaryPromptTemplates"] = {
        "LLMPromptTemplateId": {"S": "CustomSummaryPromptTemplates"},
        "*Information*": {"S": "guidance text"},
    }
    legacy.get_item = _legacy_get_item(legacy)  # type: ignore[method-assign]
    assert seeder.adopt_legacy_prompts("old-table", client=legacy) == {}


def test_no_previous_table_is_not_an_error(seeder) -> None:
    assert seeder.adopt_legacy_prompts("", client=None) == {}


class Unreadable:
    """Stands in for a previous table the seeder cannot read."""

    def get_item(self, **_kwargs):
        raise RuntimeError("no access to that table")


def test_a_previous_table_that_cannot_be_read_does_not_fail_the_deployment(seeder) -> None:
    """An empty profile is a far better outcome than a stack that rolls back."""
    adopted = seeder.adopt_legacy_prompts("old-table", client=Unreadable())
    assert adopted == {seeder.READ_FAILED_MARKER: True}


def test_a_previous_table_that_cannot_be_read_is_reported_distinctly(seeder) -> None:
    """The adoption is a one-shot, so the one chance to notice is the stack event.

    The profile it creates is then present, so no later deployment tries the
    adoption again. An operator who had edited prompts before the upgrade has to
    apply them again, and nothing but this outcome tells them to.
    """
    store, dynamo = build_store()
    outcome = seeder.seed(
        store, domain="default", legacy_table_name="old-table", legacy_client=Unreadable()
    )
    assert outcome["Profile"] == "created"
    assert outcome["ProfileOrigin"] == "created-previous-table-unreadable"
    # The marker is internal bookkeeping and must not be stored as configuration.
    assert seeder.READ_FAILED_MARKER not in dynamo.body("Config#custom")
    assert dynamo.body("Config#custom") == {}


def test_adoption_is_skipped_once_a_profile_exists(seeder) -> None:
    """Otherwise a later deployment could overwrite edits with pre-upgrade ones."""
    legacy = FakeDynamo()
    legacy.items["CustomSummaryPromptTemplates"] = {
        "LLMPromptTemplateId": {"S": "CustomSummaryPromptTemplates"},
        "1#SUMMARY": {"S": "a pre-upgrade prompt"},
    }
    legacy.get_item = _legacy_get_item(legacy)  # type: ignore[method-assign]

    store, dynamo = build_store()
    seeder.seed(store, domain="default")
    store.write_profile(
        "custom",
        {"summary_prompt_templates": {"1#SUMMARY": "written after the upgrade"}},
        updated_by="admin@example.com",
        expected_version=1,
    )
    seeder.seed(store, domain="default", legacy_table_name="old-table", legacy_client=legacy)
    assert dynamo.body("Config#custom")["summary_prompt_templates"]["1#SUMMARY"] == (
        "written after the upgrade"
    )


# ── the stack parameters that are defaults ─────────────────────────────────

MODEL_PARAMETERS = {
    "BedrockModelId": "global.anthropic.claude-sonnet-5-5",
    "MeetingAssistServiceBedrockModelID": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "MeetingAssistKnowledgeBaseModelID": "global.anthropic.claude-sonnet-4-6",
    "KeyframeTextModelId": "amazon.nova-lite-v1:0",
}


def test_the_stack_parameters_are_written_into_the_baseline(seeder) -> None:
    store, dynamo = build_store()
    outcome = seeder.seed(store, domain="default", parameter_values=MODEL_PARAMETERS)
    assert dynamo.body("Config#default")["language_models"] == {
        "summaryModelId": "global.anthropic.claude-sonnet-5-5",
        "meetingAssistModelId": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "knowledgeBaseModelId": "global.anthropic.claude-sonnet-4-6",
        "keyframeTextModelId": "amazon.nova-lite-v1:0",
    }
    assert outcome["ParameterDefaults"].split(",") == list(MODEL_PARAMETERS)
    # The profile is an administrator's. A parameter value is the deployment's
    # default, so it never lands there.
    assert "language_models" not in dynamo.body("Config#custom")


def test_an_existing_deployment_keeps_its_parameter_values_on_upgrade(seeder) -> None:
    """The upgrade path: a stack that set the parameters reads the same values after."""
    store, _dynamo = build_store()
    seeder.seed(store, domain="default")  # a release before the parameters moved
    seeder.seed(
        store, domain="default", parameter_values={"BedrockModelId": "amazon.nova-pro-v1:0"}
    )
    store.invalidate()
    assert store.get_section("language_models").values == {"summaryModelId": "amazon.nova-pro-v1:0"}


def test_an_administrators_choice_wins_over_the_parameter(seeder) -> None:
    store, _dynamo = build_store()
    seeder.seed(store, domain="default", parameter_values=MODEL_PARAMETERS)
    store.write_section(
        "custom",
        "language_models",
        {"summaryModelId": "amazon.nova-pro-v1:0"},
        updated_by="admin@example.com",
        expected_version=None,
    )
    # A later stack update that changes the parameter changes the baseline, and
    # the administrator's choice still wins over it.
    seeder.seed(
        store,
        domain="default",
        parameter_values={**MODEL_PARAMETERS, "BedrockModelId": "us.anthropic.claude-sonnet-4-6"},
    )
    store.invalidate()
    resolved = store.get_section("language_models").values
    assert resolved["summaryModelId"] == "amazon.nova-pro-v1:0"
    assert resolved["keyframeTextModelId"] == "amazon.nova-lite-v1:0"


def test_a_parameter_change_reaches_every_setting_nobody_changed(seeder) -> None:
    store, dynamo = build_store()
    seeder.seed(store, domain="default", parameter_values=MODEL_PARAMETERS)
    seeder.seed(
        store,
        domain="default",
        parameter_values={**MODEL_PARAMETERS, "KeyframeTextModelId": "amazon.nova-pro-v1:0"},
    )
    assert dynamo.body("Config#default")["language_models"]["keyframeTextModelId"] == (
        "amazon.nova-pro-v1:0"
    )


def test_without_the_property_the_section_is_empty_and_readers_use_their_environment(
    seeder,
) -> None:
    store, dynamo = build_store()
    outcome = seeder.seed(store, domain="default")
    assert dynamo.body("Config#default")["language_models"] == {}
    assert outcome["ParameterDefaults"] == "none"


def test_the_handler_passes_the_parameter_defaults_through(seeder, monkeypatch) -> None:
    dynamo = FakeDynamo()
    monkeypatch.setattr(
        seeder, "ConfigurationStore", lambda name, **_: ConfigurationStore(name, client=dynamo)
    )
    event = _event("Update")
    event["ResourceProperties"]["ParameterDefaults"] = dict(MODEL_PARAMETERS)
    seeder.handler(event, _Context())
    assert seeder.recorder.sent[-1]["status"] == "SUCCESS"
    assert (
        dynamo.body("Config#default")["language_models"]["summaryModelId"]
        == (MODEL_PARAMETERS["BedrockModelId"])
    )


# ── the CloudFormation contract ────────────────────────────────────────────


def test_a_create_answers_cloudformation_with_success(seeder, monkeypatch) -> None:
    dynamo = FakeDynamo()
    monkeypatch.setattr(
        seeder, "ConfigurationStore", lambda name, **_: ConfigurationStore(name, client=dynamo)
    )
    seeder.handler(_event("Create"), _Context())
    assert seeder.recorder.sent[-1]["status"] == "SUCCESS"
    assert "Config#default" in dynamo.items


def test_every_invocation_reports_the_same_physical_id(seeder, monkeypatch) -> None:
    """Otherwise a new content hash replaces the resource instead of updating it.

    `cfnresponse` falls back to the log stream name, which differs every time,
    so CloudFormation would see a different physical id on each run, treat the
    change as a replacement and send a Delete for the old resource. Harmless --
    the Delete branch does nothing -- but it makes the stack events read as
    though something was recreated.
    """
    dynamo = FakeDynamo()
    monkeypatch.setattr(
        seeder, "ConfigurationStore", lambda name, **_: ConfigurationStore(name, client=dynamo)
    )
    seeder.handler(_event("Create"), _Context())
    seeder.handler(_event("Update"), _Context())
    seeder.handler(_event("Delete"), _Context())
    ids = {sent["physical_id"] for sent in seeder.recorder.sent}
    assert len(ids) == 1, f"the physical id changed between invocations: {ids}"
    assert "lma-configuration" in next(iter(ids))


def test_a_delete_leaves_the_table_alone(seeder) -> None:
    """A resource replacement must not destroy what it has just written."""
    seeder.handler(_event("Delete"), _Context())
    assert seeder.recorder.sent[-1]["status"] == "SUCCESS"
    assert seeder.recorder.sent[-1]["data"] == {"Deleted": "nothing"}


def test_a_failure_is_reported_to_cloudformation_rather_than_timing_out(
    seeder, monkeypatch
) -> None:
    """An unanswered custom resource hangs the stack for an hour."""

    def explode(_name, **_kwargs):
        raise RuntimeError("the table is not there")

    monkeypatch.setattr(seeder, "ConfigurationStore", explode)
    seeder.handler(_event("Update"), _Context())
    assert seeder.recorder.sent[-1]["status"] == "FAILED"
    assert "not there" in seeder.recorder.sent[-1]["reason"]
    # The same physical id as a successful run reports. Answering a failed
    # update with a different one tells CloudFormation the resource was replaced
    # by the attempt that failed.
    assert seeder.recorder.sent[-1]["physical_id"] == "configuration-seed/lma-configuration"


def _event(request_type: str) -> Dict[str, Any]:
    return {
        "RequestType": request_type,
        "ResourceProperties": {
            "ConfigurationTableName": "lma-configuration",
            "Domain": "default",
            "LegacyPromptTemplateTableName": "",
            "SeedContentHash": "abc123",
        },
    }


class _Context:
    """A Lambda context, with a log stream name that differs per invocation.

    Faithful to the real one, and load-bearing: it is what cfnresponse falls
    back to for the physical resource id, so a constant here would let a handler
    that passes no id look stable when it is not.
    """

    _issued = 0

    def __init__(self) -> None:
        type(self)._issued += 1
        self.log_stream_name = f"a-log-stream-{type(self)._issued}"


def _legacy_get_item(fake: FakeDynamo):
    """`get_item` keyed on the previous table's own partition-key attribute."""

    def get_item(**request: Any) -> Dict[str, Any]:
        fake.requests.append({"op": "get_item", **request})
        key = request["Key"]["LLMPromptTemplateId"]["S"]
        item = fake.items.get(key)
        return {"Item": item} if item is not None else {}

    return get_item


def test_the_configuration_key_attribute_is_not_the_previous_tables(seeder) -> None:
    """Two tables, two key attributes; mixing them reads nothing, silently."""
    assert codec.KEY_ATTRIBUTE == "Configuration"
    assert seeder.LEGACY_KEY_ATTRIBUTE == "LLMPromptTemplateId"


# ── revision history ───────────────────────────────────────────────────────
#
# These use the accessor's own history-aware fakes rather than the local one
# above: what they assert is how the seeder drives the accessor's history, so
# they should fail when that history's storage changes, which a separate fake
# would not.


def build_history_store(dynamo: Any = None, s3: Any = None) -> tuple[ConfigurationStore, Any, Any]:
    the_dynamo = dynamo if dynamo is not None else HistoryDynamo()
    the_s3 = s3 if s3 is not None else FakeS3()
    store = ConfigurationStore(
        "lma-configuration", client=the_dynamo, revision_bucket=HISTORY_BUCKET, s3_client=the_s3
    )
    return store, the_dynamo, the_s3


class LegacyTables:
    """The previous per-store tables, keyed on whatever key attribute each uses."""

    def __init__(self, items: Dict[str, Dict[str, Any]]) -> None:
        self.items = items

    def get_item(self, **request: Any) -> Dict[str, Any]:
        key = next(iter(request["Key"].values()))["S"]
        item = self.items.get(key)
        return {"Item": item} if item is not None else {}


# The tables a deployment always names: CloudFormation passes all three.
LEGACY_TABLES = {
    "nova_sonic_config": "old-nova-table",
    "chat_buttons": "old-chat-table",
    "asr_engine": "old-asr-table",
}


def test_a_fresh_deployment_starts_its_history_with_one_revision(seeder) -> None:
    store, _, s3 = build_history_store()

    outcome = seeder.seed(
        store,
        domain="default",
        legacy_table_name="old-prompt-table",
        legacy_client=LegacyTables({}),
        legacy_section_tables=LEGACY_TABLES,
    )

    assert outcome["RevisionHistory"] == "custom=present"
    assert set(adoption(outcome).values()) == {"nothing-to-adopt"}
    entries = store.list_revisions("custom").entries
    assert [(entry.revision, entry.source, entry.created_by) for entry in entries] == [
        (1, "deploy", "cloudformation")
    ]
    # The one revision holds every section the seeder recorded.
    assert s3.envelope(1)["Body"] == store.read_profile("custom").body
    assert set(s3.envelope(1)["Body"]) == {"nova_sonic_config", "chat_buttons", "asr_engine"}


def test_an_upgrade_from_a_release_without_the_store_is_revision_one(seeder) -> None:
    """The upgrade operators make: from a release with no configuration store."""
    store, _, s3 = build_history_store()
    legacy = LegacyTables(
        {
            "CustomSummaryPromptTemplates": {
                "LLMPromptTemplateId": {"S": "CustomSummaryPromptTemplates"},
                "1#SUMMARY": {"S": "an admin's prompt"},
            },
            "CustomNovaSonicConfig": CUSTOMISED_NOVA_SONIC_ITEM,
        }
    )

    outcome = seeder.seed(
        store,
        domain="default",
        legacy_table_name="old-prompt-table",
        legacy_client=legacy,
        legacy_section_tables=LEGACY_TABLES,
    )

    assert outcome["RevisionHistory"] == "custom=present"
    assert outcome["ProfileOrigin"] == "created-from-previous-table"
    assert adoption(outcome)["nova_sonic_config"] == "adopted-from-previous-table"
    assert [entry.revision for entry in store.list_revisions("custom").entries] == [1]
    body = s3.envelope(1)["Body"]
    assert body["summary_prompt_templates"] == {"1#SUMMARY": "an admin's prompt"}
    assert body["nova_sonic_config"]["systemPrompt"] == "You are Sam, the team's assistant."
    # A later deployment adds nothing to the history.
    again = seeder.seed(
        store,
        domain="default",
        legacy_client=legacy,
        legacy_section_tables=LEGACY_TABLES,
    )
    assert again["RevisionHistory"] == "custom=present"
    assert set(adoption(again).values()) == {"already-present"}
    assert [entry.revision for entry in store.list_revisions("custom").entries] == [1]


def test_a_profile_with_no_history_yet_is_recorded_as_it_stands(seeder) -> None:
    """The defensive case: a profile saved by code that kept no history."""
    dynamo = HistoryDynamo()
    seeder.seed(ConfigurationStore("lma-configuration", client=dynamo), domain="default")
    ConfigurationStore("lma-configuration", client=dynamo).write_section(
        "custom",
        "summary_prompt_templates",
        {"1#SUMMARY": "an admin's prompt"},
        updated_by="admin@example.com",
        expected_version=None,
    )
    store, _, s3 = build_history_store(dynamo=dynamo)

    outcome = seeder.seed(store, domain="default")

    assert outcome["RevisionHistory"] == "custom=started"
    entry = store.list_revisions("custom").entries[0]
    assert (entry.revision, entry.source, entry.created_by) == (1, "backfill", "admin@example.com")
    assert s3.envelope(1)["Body"]["summary_prompt_templates"] == {"1#SUMMARY": "an admin's prompt"}
    assert seeder.seed(store, domain="default")["RevisionHistory"] == "custom=present"


def test_history_that_cannot_be_written_does_not_fail_the_deployment(seeder) -> None:
    # A profile with no history yet and no Nova Sonic section, so this run both
    # starts its history and adopts a section -- the two writes that need the
    # bucket.
    dynamo = HistoryDynamo()
    seeder.seed(ConfigurationStore("lma-configuration", client=dynamo), domain="default")
    legacy = LegacyTables({"CustomNovaSonicConfig": CUSTOMISED_NOVA_SONIC_ITEM})
    s3 = FakeS3()
    s3.fail_puts = 99
    store, _, _ = build_history_store(dynamo=dynamo, s3=s3)

    outcome = seeder.seed(
        store, domain="default", legacy_client=legacy, legacy_section_tables=NOVA_TABLES
    )

    assert outcome["RevisionHistory"] == "custom=not-started"
    assert adoption(outcome)["nova_sonic_config"] == "revision-history-unavailable"
    assert "nova_sonic_config" not in store.read_profile("custom").body

    # The next deployment, with the bucket writable, completes both.
    s3.fail_puts = 0
    retried = seeder.seed(
        store, domain="default", legacy_client=legacy, legacy_section_tables=NOVA_TABLES
    )
    assert retried["RevisionHistory"] == "custom=recovered"
    assert adoption(retried)["nova_sonic_config"] == "adopted-from-previous-table"
    assert store.get_revision("custom", 1).entry.source == "recovered"
    assert store.get_revision("custom", 2).body["nova_sonic_config"]["promptMode"] == "inject"


def test_the_handler_answers_success_when_history_cannot_be_written(seeder, monkeypatch) -> None:
    s3 = FakeS3()
    s3.fail_puts = 99
    dynamo = HistoryDynamo()
    monkeypatch.setattr(
        seeder,
        "ConfigurationStore",
        lambda name, **_: ConfigurationStore(
            name, client=dynamo, revision_bucket=HISTORY_BUCKET, s3_client=s3
        ),
    )

    seeder.handler(_event("Create"), _Context())

    assert seeder.recorder.sent[-1]["status"] == "SUCCESS"


def test_a_seeder_given_no_bucket_reports_it(seeder) -> None:
    store, _ = build_store()

    assert seeder.seed(store, domain="default")["RevisionHistory"] == "custom=not-configured"


def test_the_handler_passes_the_revision_bucket_through(seeder, monkeypatch) -> None:
    seen: Dict[str, Any] = {}

    def capture(name, **kwargs):
        seen.update(kwargs)
        return ConfigurationStore(name, client=FakeDynamo())

    monkeypatch.setattr(seeder, "ConfigurationStore", capture)
    event = _event("Update")
    event["ResourceProperties"]["ConfigurationBucketName"] = "the-revision-bucket"
    seeder.handler(event, _Context())

    assert seen["revision_bucket"] == "the-revision-bucket"
