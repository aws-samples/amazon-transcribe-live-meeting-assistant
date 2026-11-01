# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the meeting cost resolver (no AWS calls).

Asserted here: an estimate is answered to exactly the callers ``getCall``
answers, a stored price table has only numbers a computation can use, an
administrator's edit is a section of the profile and is refused against a
superseded version, and the estimate is computed from what is stored now.
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
LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
for path in (str(HERE), str(LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "lma-configuration")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["EVENT_SOURCING_TABLE"] = "calls"

from lma_config import codec  # noqa: E402
from lma_config.keys import ACTIVE_POINTER_KEY, DEFAULT_KEY  # noqa: E402
from lma_config.store import ConfigurationStore  # noqa: E402
from lma_metering import build_item  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("call_cost_resolver_index", HERE / "index.py")
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

ADMIN = {"claims": {"cognito:groups": ["Admin"], "email": "admin@example.com", "sub": "a"}}
OWNER = {"claims": {"sub": "o"}, "username": "owner@example.com"}
FRIEND = {"claims": {"sub": "f"}, "username": "friend@example.com"}
STRANGER = {"claims": {"sub": "x"}, "username": "x@example.com"}
BACKEND = {"userArn": "arn:aws:sts::123456789012:assumed-role/role/session", "username": "AROA"}

PRICING = {
    "currency": "USD",
    "pricesAsOf": "2026-10-28",
    "source": "test",
    "baseRegion": "us-east-1",
    "pricedRegions": ["us-east-1"],
    "regions": {"default": {"bedrock/m": {"inputTokens": 0.000001, "outputTokens": 0.000005}}},
    "nonChargeableUnits": ["requests"],
}


class FakeConfigurationTable:
    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}

    def get_item(self, **request: Any) -> Dict[str, Any]:
        item = self.items.get(request["Key"][codec.KEY_ATTRIBUTE]["S"])
        return {"Item": item} if item is not None else {}

    def put_item(self, **request: Any) -> Dict[str, Any]:
        item = request["Item"]
        key = item[codec.KEY_ATTRIBUTE]["S"]
        if request.get("ConditionExpression") == "#version = :expected":
            expected = request["ExpressionAttributeValues"][":expected"]["N"]
            if self.items.get(key, {}).get("Version", {}).get("N") != expected:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
                    "PutItem",
                )
        self.items[key] = item
        return {}

    def seed(self, key: str, body: Dict[str, Any], version: int = 1) -> None:
        self.items[key] = {
            codec.KEY_ATTRIBUTE: {"S": key},
            "Version": {"N": str(version)},
            "Body": {"B": codec.encode_body(body)},
        }


class FakeCallsTable:
    def __init__(self) -> None:
        self.call = {"Owner": {"S": "owner@example.com"}, "SharedWith": {"S": "friend@example.com"}}
        self.metering: List[Dict[str, Any]] = []
        self.gets = 0

    def get_item(self, **request: Any) -> Dict[str, Any]:
        self.gets += 1
        assert request["Key"]["PK"]["S"] == "c#Meeting-1"
        return {"Item": self.call} if self.call else {}

    def query(self, **request: Any) -> Dict[str, Any]:
        assert request["ExpressionAttributeValues"][":pk"]["S"] == "mtr#Meeting-1"
        return {"Items": self.metering}


@pytest.fixture
def tables(monkeypatch: pytest.MonkeyPatch):
    config = FakeConfigurationTable()
    config.seed(DEFAULT_KEY, {"call_pricing": PRICING})
    config.seed("Config#custom", {})
    config.items[ACTIVE_POINTER_KEY] = {
        codec.KEY_ATTRIBUTE: {"S": ACTIVE_POINTER_KEY},
        "ActiveProfile": {"S": "custom"},
    }
    calls = FakeCallsTable()
    calls.metering.append(
        build_item(
            "Meeting-1",
            "Summary",
            "bedrock/m",
            {"inputTokens": 1000, "outputTokens": 100, "requests": 1},
            source="test",
        )
    )
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore("cfg", client=config))
    monkeypatch.setattr(index, "DYNAMODB", calls)
    return config, calls


def invoke(field: str, identity: Dict[str, Any], **arguments: Any) -> Any:
    return index.handler(
        {"identity": identity, "info": {"fieldName": field}, "arguments": arguments}, None
    )


@pytest.mark.parametrize("identity", [ADMIN, OWNER, FRIEND, BACKEND])
def test_the_estimate_is_answered_to_the_callers_get_call_answers(tables, identity) -> None:
    result = invoke("getCallCost", identity, CallId="Meeting-1")
    assert result["TotalCost"] == pytest.approx(0.0015)
    assert result["Currency"] == "USD" and result["PricesAsOf"] == "2026-10-28"


#: Every shape SharedWith is stored in, each sharing with friend@example.com
#: and somebody else: the list shareCall writes, a string set, and the
#: comma-separated and "[a, b]" strings of older and created-shared meetings.
SHARED_WITH_SHAPES = {
    "list": {"L": [{"S": "friend@example.com"}, {"S": "other@example.com"}]},
    "string set": {"SS": ["friend@example.com", "other@example.com"]},
    "string": {"S": "friend@example.com, other@example.com"},
    "bracketed string": {"S": "[other@example.com, friend@example.com]"},
    "single string": {"S": "friend@example.com"},
}


@pytest.mark.parametrize("shape", sorted(SHARED_WITH_SHAPES))
@pytest.mark.parametrize("identity", [OWNER, FRIEND, ADMIN, BACKEND])
def test_every_stored_shared_with_shape_answers_the_same_callers(tables, shape, identity) -> None:
    tables[1].call = {"Owner": {"S": "owner@example.com"}, "SharedWith": SHARED_WITH_SHAPES[shape]}
    assert invoke("getCallCost", identity, CallId="Meeting-1")["TotalCost"] == pytest.approx(0.0015)


@pytest.mark.parametrize("shape", sorted(SHARED_WITH_SHAPES))
def test_every_stored_shared_with_shape_refuses_other_users(tables, shape) -> None:
    tables[1].call = {"Owner": {"S": "owner@example.com"}, "SharedWith": SHARED_WITH_SHAPES[shape]}
    # A username that is only part of an entry is not an entry.
    for identity in (STRANGER, {"claims": {"sub": "p"}, "username": "friend@example"}):
        with pytest.raises(index.ForbiddenError):
            invoke("getCallCost", identity, CallId="Meeting-1")


def test_a_meeting_shared_with_nobody_answers_only_its_owner(tables) -> None:
    tables[1].call = {"Owner": {"S": "owner@example.com"}}
    invoke("getCallCost", OWNER, CallId="Meeting-1")
    with pytest.raises(index.ForbiddenError):
        invoke("getCallCost", FRIEND, CallId="Meeting-1")


def test_a_caller_the_meeting_is_not_shared_with_is_refused(tables) -> None:
    with pytest.raises(index.ForbiddenError):
        invoke("getCallCost", STRANGER, CallId="Meeting-1")


def test_access_is_decided_by_the_username_alone_as_get_call_decides_it(tables) -> None:
    """An email claim that matches the owner does not stand in for the username."""
    impostor = {
        "claims": {
            "sub": "z",
            "email": "owner@example.com",
            "cognito:username": "owner@example.com",
        },
        "username": "someone-else",
    }
    with pytest.raises(index.ForbiddenError):
        invoke("getCallCost", impostor, CallId="Meeting-1")


def test_a_meeting_that_does_not_exist_is_refused_like_one_not_shared(tables) -> None:
    tables[1].call = None
    with pytest.raises(index.ForbiddenError):
        invoke("getCallCost", OWNER, CallId="Meeting-1")


def test_an_admin_is_not_made_to_wait_for_an_ownership_read(tables) -> None:
    invoke("getCallCost", ADMIN, CallId="Meeting-1")
    assert tables[1].gets == 0


def test_the_estimate_uses_an_administrators_price_change(tables) -> None:
    config, _ = tables
    config.seed(
        "Config#custom",
        {"call_pricing": {"regions": {"default": {"bedrock/m": {"inputTokens": 0.00001}}}}},
    )
    result = invoke("getCallCost", OWNER, CallId="Meeting-1")
    assert result["TotalCost"] == pytest.approx(0.01 + 0.0005)


@pytest.mark.parametrize("identity", [OWNER, BACKEND])
def test_the_price_table_is_only_for_admins(tables, identity) -> None:
    with pytest.raises(index.ForbiddenError):
        invoke("getCallPricing", identity)
    with pytest.raises(index.ForbiddenError):
        invoke("updateCallPricing", identity, input={"Pricing": "{}", "ExpectedVersion": 1})


def test_the_editor_reads_the_baseline_the_change_and_the_result(tables) -> None:
    payload = json.loads(invoke("getCallPricing", ADMIN)["Pricing"])
    assert payload["Default"]["currency"] == "USD"
    assert payload["Custom"] == {}
    assert payload["Effective"]["regions"]["default"]["bedrock/m"]["inputTokens"] == 0.000001
    assert payload["Version"] == 1


def test_a_saved_change_is_the_pricing_section_of_the_profile(tables) -> None:
    config, _ = tables
    delta = {"regions": {"default": {"bedrock/m": {"inputTokens": 0.000002}}}}
    result = invoke(
        "updateCallPricing", ADMIN, input={"Pricing": json.dumps(delta), "ExpectedVersion": 1}
    )
    assert result == {"Success": True, "Version": 2}
    stored = codec.decode_body(config.items["Config#custom"]["Body"]["B"])
    assert stored == {"call_pricing": delta}


def test_a_save_that_names_no_version_is_refused(tables) -> None:
    """The save replaces the whole section, so it must be pinned to what was read."""
    with pytest.raises(ValueError, match="ExpectedVersion"):
        index.update_call_pricing({"Pricing": "{}"}, index._caller(ADMIN))


def test_a_save_against_a_superseded_version_is_refused(tables) -> None:
    with pytest.raises(Exception, match="Conflict"):
        invoke("updateCallPricing", ADMIN, input={"Pricing": "{}", "ExpectedVersion": 7})


@pytest.mark.parametrize(
    "price",
    ["0.1", -1, float("inf"), True, None, {"nested": 1}],
)
def test_a_price_that_is_not_a_non_negative_number_is_refused(tables, price) -> None:
    delta = {"regions": {"default": {"bedrock/m": {"inputTokens": price}}}}
    with pytest.raises(ValueError):
        index.update_call_pricing({"Pricing": delta, "ExpectedVersion": 1}, index._caller(ADMIN))


@pytest.mark.parametrize(
    "delta",
    [
        {"notes": []},
        {"notes": "x"},
        {"notes": {"bedrock/m": 1}},
        {"notIncluded": [1]},
        {"pricedRegions": "us-east-1"},
        {"currency": 1},
        {"pricesAsOf": None},
        {"source": ["a"]},
        {"baseRegion": {}},
        {"regionz": {}},
    ],
)
def test_a_table_the_estimator_could_not_read_is_refused(tables, delta) -> None:
    """Each of these, stored, would make every later estimate fail or mislead."""
    with pytest.raises(ValueError):
        index.update_call_pricing({"Pricing": delta, "ExpectedVersion": 1}, index._caller(ADMIN))


def test_a_valid_change_to_every_kind_of_key_is_accepted(tables) -> None:
    delta = {
        "currency": "USD",
        "pricesAsOf": "2026-11-01",
        "source": "negotiated",
        "baseRegion": "us-east-1",
        "pricedRegions": ["us-east-1"],
        "regions": {"default": {"bedrock/m": {"inputTokens": 0.0000005}}},
        "nonChargeableUnits": ["requests"],
        "notes": {"bedrock/m": "private rate"},
        "notIncluded": ["NAT gateways"],
    }
    result = index.update_call_pricing(
        {"Pricing": json.dumps(delta), "ExpectedVersion": 1}, index._caller(ADMIN)
    )
    assert result["Success"] is True


def test_an_unknown_field_is_refused(tables) -> None:
    with pytest.raises(ValueError):
        invoke("deleteEverything", ADMIN)


def test_a_comment_on_the_save_is_passed_to_the_revision_it_records(
    tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: Dict[str, Any] = {}

    def write_section(*_args: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return 2

    monkeypatch.setattr(index.CONFIG_STORE, "write_section", write_section)

    invoke(
        "updateCallPricing", ADMIN, input={"Pricing": "{}", "ExpectedVersion": 1, "Comment": "why"}
    )

    assert seen["comment"] == "why"
