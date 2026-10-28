#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for the per-meeting cost estimate and its price table.

Serves three fields, dispatching on the field name AppSync reports:

``Query.getCallCost(CallId)``
    The itemised estimate for one meeting, computed on every read from the
    meeting's metering records and the price table in force. Answered for the
    same callers ``getCall`` answers: an Admin, the meeting's owner, somebody it
    is shared with, or a backend role signing with IAM.

``Query.getCallPricing``
    The price table for the calling Admin: the shipped baseline, the Admin's
    changes over it, the merged result, and the version to send back on save.
    Admin only.

``Mutation.updateCallPricing(input)``
    Store the administrator's changes as the ``call_pricing`` section of the
    ``custom`` profile. The section is a delta over the shipped table, so a
    release that updates a price still reaches every price nobody changed.
    Admin only, and conditional on the `Version` getCallPricing returned.

Why on read rather than at meeting end: the usage of a meeting keeps arriving
after it ends -- the summary is generated after the END event, the Virtual
Participant writes its runtime when it leaves, a summary can be regenerated
days later -- so a figure frozen at END would be wrong for exactly the meetings
people look at. A meeting's records are one ``Query`` on one partition.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Dict, Mapping, Optional

import boto3
from botocore.config import Config as BotoCoreConfig

# pylint: disable=import-error
from lma_config import (  # from the configuration layer
    CALL_PRICING,
    ConcurrentModification,
    ConfigurationStore,
    deep_merge,
)
from lma_metering import estimate_call_cost, read_records

# pylint: enable=import-error

# Stdlib logging rather than aws_lambda_powertools: this function carries only
# the configuration layer, which is pure Python on the runtime's own boto3 -- the
# same choice the configuration seeder makes.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)

EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
DEPLOYMENT_REGION = os.environ.get("AWS_REGION", "us-east-1")

OPERATOR_PROFILE = "custom"
GET_COST_FIELD = "getCallCost"
GET_PRICING_FIELD = "getCallPricing"
UPDATE_PRICING_FIELD = "updateCallPricing"

# Built once per container: the store's cache lives on the instance.
CONFIG_STORE = ConfigurationStore()


class ForbiddenError(Exception):
    """The caller may not see or change what was asked for."""


def _caller(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Who is asking, from the AppSync ``identity`` block only.

    A request signed with IAM by one of LMA's own roles carries a ``userArn``
    naming an assumed role and no user-pool claims; it is treated as the backend,
    exactly as ``getCall``'s resolver treats it. AppSync's IAM grants decide which
    roles can reach this field at all.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    user_arn = str(identity.get("userArn") or "")
    is_backend = not claims and ":assumed-role/" in user_arn
    return {
        # identity.username alone: the one value getCall.response.vtl compares
        # Owner and SharedWith with, so both fields answer the same callers.
        "username": str(identity.get("username") or ""),
        "subject": claims.get("sub") or identity.get("sub") or "",
        "actor": claims.get("email") or claims.get("cognito:username") or identity.get("username"),
        "is_admin": ADMIN_GROUP in groups,
        "is_backend": is_backend,
    }


def _shared_with(raw: Any) -> set:
    if isinstance(raw, str):
        return {s.strip() for s in raw.split(",") if s.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(s).strip() for s in raw if s}
    return set()


def _require_call_access(caller: Mapping[str, Any], call_id: str) -> None:
    """The ``getCall`` rule: Admin, owner, shared-with, or the backend."""
    if caller["is_admin"] or caller["is_backend"]:
        return
    response = DYNAMODB.get_item(
        TableName=EVENT_SOURCING_TABLE,
        Key={"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}},
        ProjectionExpression="#o, SharedWith",
        ExpressionAttributeNames={"#o": "Owner"},
    )
    item = response.get("Item") or {}
    owner = (item.get("Owner") or {}).get("S", "")
    shared_raw = item.get("SharedWith") or {}
    shared = _shared_with(shared_raw.get("S") or shared_raw.get("SS") or [])
    username = caller["username"]
    if username and (owner == username or username in shared):
        return
    # The same answer for a meeting that does not exist as for one the caller
    # may not see, so the field cannot be used to test which meetings exist.
    LOGGER.info("refused a cost estimate for %s to %s", call_id, caller["subject"] or "(none)")
    raise ForbiddenError("Unauthorized: you do not have access to this meeting")


def _require_admin(caller: Mapping[str, Any]) -> None:
    if not caller["is_admin"]:
        raise ForbiddenError(f"Unauthorized: only members of the {ADMIN_GROUP} group can do this")


def get_call_cost(call_id: str) -> Dict[str, Any]:
    """The estimate for one meeting, in the ``CallCost`` GraphQL shape."""
    records = read_records(DYNAMODB, EVENT_SOURCING_TABLE, call_id)
    pricing = CONFIG_STORE.get_section(CALL_PRICING.name).values
    result = estimate_call_cost(call_id, records, pricing, DEPLOYMENT_REGION)
    LOGGER.info(
        "estimated %s: %d record(s), %d line(s), %d unpriced, total %s",
        call_id,
        result["RecordCount"],
        len(result["Items"]),
        result["UnpricedCount"],
        result["TotalCost"],
    )
    return result


def get_call_pricing() -> Dict[str, Any]:
    """The table for an Admin. Read from the stored records, never the cache."""
    baseline = CONFIG_STORE.read_profile("default", consistent_read=True)
    profile = CONFIG_STORE.read_profile(OPERATOR_PROFILE, consistent_read=True)
    default_section = baseline.body.get(CALL_PRICING.name) or {}
    custom_section = profile.body.get(CALL_PRICING.name) or {}
    # The merge the store applies on every read, so "effective" is what an
    # estimate drawn now would use.
    payload = {
        "Default": default_section,
        "Custom": custom_section,
        "Effective": deep_merge(default_section, custom_section),
        "Version": profile.version,
        "UpdatedBy": profile.updated_by or None,
        "UpdatedAt": profile.updated_at or None,
        "Region": DEPLOYMENT_REGION,
    }
    return {"Pricing": json.dumps(payload)}


#: The top-level keys a price table has. Anything else is refused, because the
#: estimator would ignore it and the Admin who saved it would not know.
KNOWN_PRICING_KEYS = {
    "currency",
    "pricesAsOf",
    "source",
    "baseRegion",
    "pricedRegions",
    "regions",
    "nonChargeableUnits",
    "notes",
    "notIncluded",
}


def _validate_pricing_delta(delta: Any) -> Dict[str, Any]:
    """Refuse a price table the estimator could not use, before it is stored.

    A price must be a finite, non-negative number: a string or a negative value
    stored here would make every estimate wrong at once, with the error far from
    the save that caused it.
    """
    if not isinstance(delta, dict):
        raise ValueError("Pricing must be a JSON object")
    regions = delta.get("regions", {})
    if not isinstance(regions, dict):
        raise ValueError("'regions' must be an object of region -> service -> unit -> price")
    for region, services in regions.items():
        if not isinstance(services, dict):
            raise ValueError(f"regions.{region} must be an object of service -> unit -> price")
        for service, units in services.items():
            if not isinstance(units, dict):
                raise ValueError(f"regions.{region}.{service} must be an object of unit -> price")
            for unit, price in units.items():
                if (
                    isinstance(price, bool)
                    or not isinstance(price, (int, float))
                    or not math.isfinite(price)
                    or price < 0
                ):
                    raise ValueError(
                        f"regions.{region}.{service}.{unit} must be a non-negative number; "
                        f"got {price!r}"
                    )
    for key in ("nonChargeableUnits", "pricedRegions", "notIncluded"):
        if key in delta and (
            not isinstance(delta[key], list) or not all(isinstance(v, str) for v in delta[key])
        ):
            raise ValueError(f"'{key}' must be a list of strings")
    notes = delta.get("notes", {})
    if not isinstance(notes, dict) or not all(isinstance(v, str) for v in notes.values()):
        raise ValueError("'notes' must be an object of service -> text")
    for key in ("currency", "pricesAsOf", "source", "baseRegion"):
        if key in delta and not isinstance(delta[key], str):
            raise ValueError(f"'{key}' must be a string")
    unknown = sorted(set(delta) - KNOWN_PRICING_KEYS)
    if unknown:
        raise ValueError(f"unknown price-table keys: {unknown}")
    return delta


def update_call_pricing(input_data: Mapping[str, Any], caller: Mapping[str, Any]) -> Dict[str, Any]:
    raw = input_data.get("Pricing")
    try:
        delta = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as error:
        raise ValueError(f"Pricing is not valid JSON: {error}") from error
    delta = _validate_pricing_delta(delta if delta is not None else {})
    # Required, unlike on the older configuration fields, which keep it optional
    # only for clients written before it existed. The save replaces the whole
    # section with this delta, so without the version a save made from a stale
    # read would silently drop another Admin's price changes.
    expected = input_data.get("ExpectedVersion")
    if expected is None:
        raise ValueError("ExpectedVersion is required: send the Version getCallPricing returned")
    version = CONFIG_STORE.write_section(
        OPERATOR_PROFILE,
        CALL_PRICING.name,
        delta,
        updated_by=str(caller["actor"] or caller["subject"] or "unknown"),
        expected_version=int(expected),
    )
    LOGGER.info("saved the meeting price table as version %d", version)
    return {"Success": True, "Version": version}


def _field(event: Mapping[str, Any]) -> str:
    field = ((event.get("info") or {}).get("fieldName") or "").strip()
    if field not in (GET_COST_FIELD, GET_PRICING_FIELD, UPDATE_PRICING_FIELD):
        raise ValueError(f"this resolver does not serve {field or '(nothing)'}")
    return field


def handler(event: Dict[str, Any], context: Any) -> Optional[Dict[str, Any]]:
    """AppSync entry point."""
    field = _field(event)
    caller = _caller(event)
    arguments = event.get("arguments") or {}

    if field == GET_COST_FIELD:
        call_id = str(arguments.get("CallId") or "")
        if not call_id:
            raise ValueError("CallId is required")
        _require_call_access(caller, call_id)
        return get_call_cost(call_id)

    _require_admin(caller)
    if field == GET_PRICING_FIELD:
        return get_call_pricing()
    try:
        return update_call_pricing(arguments.get("input") or {}, caller)
    except ConcurrentModification as error:
        raise Exception(f"Conflict: {error}") from error  # pylint: disable=broad-exception-raised
