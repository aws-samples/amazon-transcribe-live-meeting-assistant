#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Administering consents given in advance: listing them, passing a withdrawal on, erasing them.

Consents given in advance are kept under each person's address (``PK =
pmc#<address>``, ``SK = ac#<consent id>``) and carry ``ItemType =
advance_consent``, so the meetings table's ``TypeDateIndex`` lists every one of
them, newest first, without reading anyone's partition by name.

Erasing a person's consents, for an erasure request or with the account's
deletion, removes the consent items and keeps an audit entry that holds no
consent content: who erased, when, the address, the consent ids and how many
(``PK = cnsaudit#<YYYY-MM>``). A consent still in force is withdrawn first and
the withdrawal passed to the meetings in progress that counted it, so their
capture stops as it does when the person withdraws. The per-meeting consent
events (each meeting's ``Consent`` record) are not touched: they stay with
their meetings and follow the meeting's retention.

Every function takes a low-level DynamoDB client (and, to pass a withdrawal
on, a Lambda client), so the advance consent resolver and user management
erase alike. See docs/meeting-consent.md#administering-consents-given-in-advance.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from . import advance

#: The meetings table's attribute and value that put a consent in TypeDateIndex.
ITEM_TYPE = "advance_consent"
INDEX_NAME = "TypeDateIndex"
AUDIT_PREFIX = "cnsaudit#"
#: Why a consent was withdrawn, as WithdrawnReason records it.
REASON_ADMIN = "withdrawn_by_admin"
REASON_ERASED = "erased"
#: The most meetings a withdrawal is passed on to, the most recently noted first.
MAX_WITHDRAWAL_MEETINGS = 50
#: The most consents one erasure removes (a person has at most a few hundred).
MAX_ERASED = 1000
MAX_NOTE_LENGTH = 500

LOGGER = logging.getLogger(__name__)


class ConsentsUnreadable(Exception):
    """Some consents could not be read; the answer would be incomplete."""


_DES = TypeDeserializer()
_SER = TypeSerializer()


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted(_plain(v) for v in value)
    return value


def item_of(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """A DynamoDB item as plain Python values."""
    return {key: _plain(_DES.deserialize(value)) for key, value in raw.items()}


def partition(email: str) -> str:
    return f"pmc#{email.strip().casefold()}"


def consents_of(client: Any, table: str, email: str) -> List[Dict[str, Any]]:
    """Every consent kept under one address, newest first, at most MAX_ERASED of them."""
    return _consents_read(client, table, email)[0]


def _consents_read(client: Any, table: str, email: str) -> tuple:
    """The first MAX_ERASED consents under one address, and whether any were left unread."""
    arguments: Dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :ac)",
        "ExpressionAttributeValues": {":pk": {"S": partition(email)}, ":ac": {"S": "ac#"}},
        "ScanIndexForward": False,
        "ConsistentRead": True,
    }
    out: List[Dict[str, Any]] = []
    while True:
        response = client.query(**arguments)
        out += [item_of(item) for item in response.get("Items") or []]
        if len(out) > MAX_ERASED:
            return out[:MAX_ERASED], True
        if not response.get("LastEvaluatedKey"):
            return out, False
        if len(out) == MAX_ERASED:
            # A last page can be empty, so whether one is left takes one more read.
            arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            arguments["Limit"] = 1
            return out, bool(client.query(**arguments).get("Items"))
        arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def in_force(consent: Mapping[str, Any], now: datetime) -> bool:
    return advance.status_of(consent, now) == advance.STATUS_ACTIVE


def withdraw(
    client: Any,
    table: str,
    consent: Mapping[str, Any],
    *,
    now: datetime,
    reason: str,
    expires_after: int,
    by: str = "",
    note: str = "",
) -> Optional[Dict[str, Any]]:
    """Mark one consent withdrawn; the item as written, or None when it was not in force."""
    sets = "WithdrawnAt = :w, WithdrawnReason = :r, ExpiresAfter = :e"
    values: Dict[str, Any] = {
        ":w": {"S": advance.iso(now)},
        ":r": {"S": reason},
        ":e": {"N": str(int(expires_after))},
    }
    if by:
        sets += ", WithdrawnBy = :b"
        values[":b"] = {"S": by[:256]}
    if note:
        sets += ", WithdrawnNote = :n"
        values[":n"] = {"S": note[:MAX_NOTE_LENGTH]}
    try:
        response = client.update_item(
            TableName=table,
            Key={"PK": {"S": consent["PK"]}, "SK": {"S": consent["SK"]}},
            UpdateExpression=f"SET {sets}",
            ConditionExpression=(
                "attribute_exists(PK) AND attribute_not_exists(WithdrawnAt) AND ValidUntil > :w"
            ),
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except client.exceptions.ConditionalCheckFailedException:
        return None
    return item_of(response.get("Attributes") or {})


def pass_withdrawal_on(
    lambda_client: Any, function: str, consent: Mapping[str, Any], now: datetime
) -> int:
    """Tell each meeting that counted the consent that it was withdrawn; how many were told.

    The meeting consent resolver records an objection from the attendee in each
    meeting still in progress, which stops capture there. Asynchronous, so the
    caller answers at once. A meeting that cannot be told is logged and
    skipped; it keeps counting the consent until it ends.
    """
    noted = consent.get("AppliedAt") or {}
    if not function or not isinstance(noted, Mapping) or not noted:
        return 0
    consent_ids = [str(consent.get("ConsentId"))] + list(consent.get("Supersedes") or [])
    meetings = sorted(noted, key=lambda call_id: str(noted[call_id]), reverse=True)
    told = 0
    for call_id in meetings[:MAX_WITHDRAWAL_MEETINGS]:
        payload = {
            "advanceConsentWithdrawn": {
                "CallId": call_id,
                "ConsentId": consent.get("ConsentId"),
                "ConsentIds": consent_ids,
                "WithdrawnAt": advance.iso(now),
            }
        }
        try:
            lambda_client.invoke(
                FunctionName=function, InvocationType="Event", Payload=json.dumps(payload).encode()
            )
            told += 1
        except (ClientError, BotoCoreError):
            LOGGER.exception("could not pass a withdrawal on to a meeting that counted it")
    return told


def erase(  # pylint: disable=too-many-arguments,too-many-locals
    client: Any,
    table: str,
    *,
    email: str,
    erased_by: str,
    now: datetime,
    expires_after: int,
    consent_ids: Optional[Iterable[str]] = None,
    subject: str = "",
    reason: str = "admin_request",
    pass_on: Optional[Any] = None,
) -> Dict[str, Any]:
    """Permanently remove consents kept under ``email`` and write the audit entry.

    ``consent_ids`` names the consents (others under the address are left);
    without it every consent under the address is removed, or, with
    ``subject``, every one the account with that Cognito sub gave. A consent in
    force is withdrawn first (reason ``erased``) and ``pass_on(consent)`` is
    called with it, to stop capture in the meetings that counted it. Answers
    ``Deleted``, ``Withdrawn``, the ``ConsentIds`` removed, the ``AuditId`` and
    ``MoreRemain``.

    Named consents are read by their keys. Otherwise one erasure reads at most
    MAX_ERASED consents under the address; when more are kept there,
    ``MoreRemain`` is true and the audit entry's status is ``partial``, with
    the count of those removed: erasing again removes the next ones.
    """
    if consent_ids is not None:
        keys = [
            {"PK": {"S": partition(email)}, "SK": {"S": f"ac#{i}"}}
            for i in dict.fromkeys(str(i) for i in consent_ids)
        ]
        read, more_remain = list(read_items(client, table, keys).values()), False
    else:
        read, more_remain = _consents_read(client, table, email)
    targets = [c for c in read if not subject or c.get("Subject") == subject]
    withdrawn = 0
    for consent in targets:
        if in_force(consent, now):
            written = withdraw(
                client,
                table,
                consent,
                now=now,
                reason=REASON_ERASED,
                expires_after=expires_after,
                by=erased_by,
            )
            if written is not None:
                withdrawn += 1
                if pass_on is not None:
                    pass_on(written)
    audit_id = f"{now.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(6)}"
    removed = [str(c.get("ConsentId")) for c in targets]
    audit = {
        "PK": f"{AUDIT_PREFIX}{now.strftime('%Y-%m')}",
        "SK": f"{advance.iso(now)}#{audit_id}",
        "AuditId": audit_id,
        "Action": "erase_advance_consents",
        "Reason": reason,
        "ErasedBy": erased_by[:256],
        "ErasedAt": advance.iso(now),
        "Email": email.strip().casefold(),
        "ConsentIds": removed,
        "Count": len(removed),
        "Withdrawn": withdrawn,
        # "pending" until every item is deleted, so an erasure that stops part
        # way is on record with the consents it was removing.
        "Status": "pending",
    }
    client.put_item(TableName=table, Item={k: _SER.serialize(v) for k, v in audit.items()})
    for consent in targets:
        client.delete_item(
            TableName=table, Key={"PK": {"S": consent["PK"]}, "SK": {"S": consent["SK"]}}
        )
    audit["Status"] = "partial" if more_remain else "complete"
    if more_remain:
        audit["MoreRemain"] = True
    client.put_item(TableName=table, Item={k: _SER.serialize(v) for k, v in audit.items()})
    return {
        "Deleted": len(removed),
        "Withdrawn": withdrawn,
        "ConsentIds": removed,
        "AuditId": audit_id,
        "MoreRemain": more_remain,
    }


def index_key(item: Mapping[str, Any]) -> Dict[str, Dict[str, str]]:
    """A consent's key in TypeDateIndex, as a page's next key."""
    return {"ItemType": {"S": ITEM_TYPE}, "SK": {"S": item["SK"]}, "PK": {"S": item["PK"]}}


def read_items(client: Any, table: str, keys: List[Dict[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
    """The full items for index entries, by (PK, SK)."""
    found: Dict[tuple, Dict[str, Any]] = {}
    for start in range(0, len(keys), 100):
        request = {table: {"Keys": keys[start : start + 100], "ConsistentRead": True}}
        for _attempt in range(6):
            response = client.batch_get_item(RequestItems=request)
            for raw in response.get("Responses", {}).get(table, []):
                item = item_of(raw)
                found[(item["PK"], item["SK"])] = item
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
            time.sleep(0.05 * 2**_attempt)
        if request:
            # A page missing consents would drop them from the list and the export.
            raise ConsentsUnreadable("some consents could not be read; try again")
    return found
