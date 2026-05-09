# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
AppSync Lambda resolver for `listCallsDateRange` and `getCallCount`.

Uses the `TypeDateIndex` GSI on EventSourcingTable for efficient queries
instead of scanning the table.

- listCallsDateRange: Paginated query on the GSI, enriched with the
  corresponding call-detail items via BatchGetItem so the client gets
  full Call objects in a single round-trip.
- getCallCount: Paginated COUNT query on the GSI.

RBAC: we apply Owner / SharedWith filtering post-query, matching the
behaviour of the deprecated listCalls* VTL resolvers (see the old
`listCalls.response.vtl` which filtered by $identity).

Performance: O(matched items) instead of O(total table items).
Scaling: single `ItemType="call"` hot partition is acceptable at
expected volumes (<< DDB per-partition read limit in on-demand mode)
since each item is tiny (~200 B).  Can be sharded later without a
schema break by changing ItemType to `call#<YYYY-MM>`.
"""

import base64
import json
import logging
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")

TYPE_DATE_INDEX = "TypeDateIndex"
ITEM_TYPE_CALL = "call"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# DynamoDB BatchGetItem hard limit
BATCH_GET_LIMIT = 100


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal objects from DynamoDB."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def _encode_token(last_key):
    if not last_key:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(last_key, cls=DecimalEncoder).encode("utf-8")
    ).decode("ascii")


def _decode_token(token):
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("Invalid nextToken %r: %s", token, e)
        return None


def _get_caller_identity(event):
    """Extract caller identity from AppSync event for RBAC filtering."""
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    # AppSync ties identity.username to the Cognito sub-claim for IAM-authed
    # requests; for Cognito User Pools it's often the email. We try several
    # sources so the filter works in all call paths.
    username = claims.get("cognito:username") or identity.get("username") or claims.get("sub") or ""
    email = claims.get("email") or ""
    return {
        "username": username,
        "email": email,
        "groups": groups,
        "is_admin": "Admin" in groups,
    }


def _call_visible_to(caller, detail_item):
    """Match the previous listCalls.response.vtl filter: allow if caller is
    owner or listed in SharedWith. Admins see everything.
    """
    if caller["is_admin"]:
        return True

    owner = detail_item.get("Owner") or ""
    shared_with_raw = detail_item.get("SharedWith")
    # Legacy rows may store SharedWith as comma-separated string.
    shared_with = []
    if isinstance(shared_with_raw, str):
        shared_with = [s.strip() for s in shared_with_raw.split(",") if s.strip()]
    elif isinstance(shared_with_raw, list):
        shared_with = [str(s).strip() for s in shared_with_raw if s]

    identities = {i for i in (caller["username"], caller["email"]) if i}
    if owner in identities:
        return True
    return any(sw in identities for sw in shared_with)


def _batch_get_call_details(table, call_ids):
    """BatchGetItem on `c#<CallId>` detail rows. Handles 100-item batches
    and unprocessed keys retry.
    """
    if not call_ids:
        return {}

    results = {}
    keys = [{"PK": f"c#{cid}", "SK": f"c#{cid}"} for cid in call_ids]

    # Process in chunks of BATCH_GET_LIMIT
    table_name = table.table_name
    client = boto3.resource("dynamodb").meta.client
    for i in range(0, len(keys), BATCH_GET_LIMIT):
        batch = keys[i : i + BATCH_GET_LIMIT]
        request = {table_name: {"Keys": batch}}
        # Retry unprocessed keys a few times
        for _attempt in range(5):
            response = client.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(table_name, []):
                pk = item.get("PK", "")
                if pk.startswith("c#"):
                    results[pk[2:]] = item
            unprocessed = response.get("UnprocessedKeys", {}).get(table_name)
            if not unprocessed or not unprocessed.get("Keys"):
                break
            request = {table_name: unprocessed}
    return results


def _merge_list_into_call(list_item, detail):
    """Overlay the call-detail item onto the GSI list-item, filling any
    gaps (e.g. if a detail row is missing) from the GSI projection.
    """
    merged = dict(list_item) if list_item else {}
    if detail:
        merged.update(detail)
    # Ensure list keys (ListPK / ListSK) are exposed so the UI can call
    # shareCall / deleteCall which require the list-row coordinates.
    merged["ListPK"] = list_item.get("PK")
    merged["ListSK"] = list_item.get("SK")
    return merged


def _sk_bound(iso):
    """Build an SK-format lower/upper bound from an ISO-8601 datetime.

    list-tracking items use SK = "ts#<ISO8601>#id#<CallId>".  Because this is
    lexicographic-sortable, we prefix the ISO value with "ts#" to produce a
    boundary value that DynamoDB can compare against the SK range key.
    """
    return f"ts#{iso}"


def _build_key_condition(start_dt, end_dt):
    base = Key("ItemType").eq(ITEM_TYPE_CALL)
    if start_dt and end_dt:
        # Use "{end}#~" as the upper bound so rows at exactly end_dt are
        # included (SK is `ts#<iso>#id#<CallId>`; "~" sorts after "#").
        return base & Key("SK").between(_sk_bound(start_dt), f"{_sk_bound(end_dt)}#~")
    if start_dt:
        return base & Key("SK").gte(_sk_bound(start_dt))
    if end_dt:
        return base & Key("SK").lte(f"{_sk_bound(end_dt)}#~")
    return base


def handler(event, context):
    """Route to the appropriate field handler."""
    field = (event.get("info") or {}).get("fieldName", "")
    logger.info("Resolver invoked for field: %s", field)
    if field == "listCallsDateRange":
        return list_calls_date_range(event)
    if field == "getCallCount":
        return get_call_count(event)
    raise ValueError(f"Unknown field: {field}")


def list_calls_date_range(event):
    """Paginated GSI query + BatchGetItem enrichment + RBAC filter."""
    args = event.get("arguments") or {}
    start_dt = args.get("startDateTime")
    end_dt = args.get("endDateTime")
    limit = min(int(args.get("limit") or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
    next_token = args.get("nextToken")

    table_name = os.environ["EVENT_SOURCING_TABLE_NAME"]
    table = dynamodb.Table(table_name)

    caller = _get_caller_identity(event)
    logger.info(
        "Caller: username=%s email=%s groups=%s admin=%s",
        caller["username"],
        caller["email"],
        caller["groups"],
        caller["is_admin"],
    )

    collected = []  # list of enriched Call dicts post-RBAC
    last_key = _decode_token(next_token)
    pages = 0
    # Bound the number of GSI pages we fetch per client request so a very
    # restrictive RBAC filter can't produce unbounded server latency.
    MAX_PAGES = 10

    while len(collected) < limit and pages < MAX_PAGES:
        query_kwargs = {
            "IndexName": TYPE_DATE_INDEX,
            "KeyConditionExpression": _build_key_condition(start_dt, end_dt),
            "Limit": limit,
            "ScanIndexForward": False,  # newest first
        }
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key

        logger.info("GSI query page %d, last_key=%s", pages, last_key is not None)
        try:
            response = table.query(**query_kwargs)
        except Exception as e:  # noqa: BLE001
            logger.error("GSI query failed: %s", e)
            raise

        list_items = response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        pages += 1
        logger.info(
            "Page %d: %d list items, has-more=%s",
            pages,
            len(list_items),
            last_key is not None,
        )

        if not list_items:
            if not last_key:
                break
            continue

        # Fetch call-detail rows in one batch
        call_ids = [it["CallId"] for it in list_items if it.get("CallId")]
        details = _batch_get_call_details(table, call_ids)

        for list_item in list_items:
            call_id = list_item.get("CallId")
            detail = details.get(call_id, {})
            merged = _merge_list_into_call(list_item, detail)
            if not _call_visible_to(caller, merged):
                continue
            collected.append(merged)
            if len(collected) >= limit:
                break

        if not last_key:
            break

    result = {
        "Calls": collected[:limit],
        "nextToken": _encode_token(last_key) if last_key else None,
    }
    logger.info(
        "Returning %d calls, nextToken=%s, pages=%d",
        len(result["Calls"]),
        "yes" if result["nextToken"] else "no",
        pages,
    )
    return result


# Non-admin count path caps pages so callers with very broad ranges can't
# force unbounded work.  Each page is up to 1 MB of projected attributes
# (~5-10k items at ~100-200B/row) so 50 pages = ~250k-500k items.
MAX_COUNT_PAGES_NON_ADMIN = 50
# Admin count path uses Select="COUNT" which is much cheaper per page; we
# still cap to prevent runaway pagination on pathological ranges.
MAX_COUNT_PAGES_ADMIN = 500


def get_call_count(event):
    """Count matching list items on the GSI, applying RBAC.

    - Admin callers: uses DynamoDB ``Select="COUNT"`` on the GSI — no
      per-item work, cheap regardless of range size.
    - Non-admin callers: queries the GSI with projected attributes
      (``Owner``, ``SharedWith`` are in the INCLUDE projection so no
      BatchGet is needed) and counts only rows the caller is entitled
      to see.

    The response includes a ``truncated`` flag indicating whether the
    page cap was hit before exhausting the range; clients should display
    something like "500+" in that case.
    """
    args = event.get("arguments") or {}
    start_dt = args.get("startDateTime")
    end_dt = args.get("endDateTime")

    table_name = os.environ["EVENT_SOURCING_TABLE_NAME"]
    table = dynamodb.Table(table_name)

    caller = _get_caller_identity(event)
    key_condition = _build_key_condition(start_dt, end_dt)

    total = 0
    pages = 0
    last_key = None

    if caller["is_admin"]:
        query_kwargs = {
            "IndexName": TYPE_DATE_INDEX,
            "KeyConditionExpression": key_condition,
            "Select": "COUNT",
        }
        while pages < MAX_COUNT_PAGES_ADMIN:
            if last_key:
                query_kwargs["ExclusiveStartKey"] = last_key
            response = table.query(**query_kwargs)
            total += response.get("Count", 0)
            last_key = response.get("LastEvaluatedKey")
            pages += 1
            if not last_key:
                break
    else:
        # Non-admin: need per-row Owner / SharedWith inspection.  The GSI
        # projection already includes both so we don't BatchGet.
        query_kwargs = {
            "IndexName": TYPE_DATE_INDEX,
            "KeyConditionExpression": key_condition,
            "Select": "ALL_PROJECTED_ATTRIBUTES",
        }
        while pages < MAX_COUNT_PAGES_NON_ADMIN:
            if last_key:
                query_kwargs["ExclusiveStartKey"] = last_key
            response = table.query(**query_kwargs)
            for item in response.get("Items", []):
                if _call_visible_to(caller, item):
                    total += 1
            last_key = response.get("LastEvaluatedKey")
            pages += 1
            if not last_key:
                break

    truncated = bool(last_key)
    logger.info(
        "Call count: %d (range=%s..%s, admin=%s, pages=%d, truncated=%s)",
        total,
        start_dt,
        end_dt,
        caller["is_admin"],
        pages,
        truncated,
    )
    return {"count": total, "truncated": truncated}
