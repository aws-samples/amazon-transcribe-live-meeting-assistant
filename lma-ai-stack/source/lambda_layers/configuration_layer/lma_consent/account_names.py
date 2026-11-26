#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The names an LMA account goes by in meetings: a display name and short names.

A meeting's attendees are matched to its participant list by display name (see
:func:`lma_consent.advance.match_listed`). Besides the name a list entry gives
a person, each account can carry names of its own, which apply in every
meeting that lists the account's address:

``DisplayName``
    At most :data:`MAX_DISPLAY_NAME_LENGTH` characters. When the account has
    none, the user pool's standard ``name`` attribute, when set, stands in.
``ShortNames``
    At most :data:`MAX_SHORT_NAMES` names of at most
    :data:`MAX_SHORT_NAME_LENGTH` characters each: the other names the person
    shows in meetings ("Bob" for "Robert Smith").

Each name is trimmed; control characters, ``<`` and ``>`` are refused; a name
repeated, compared ignoring case and spacing as attendees are matched, is kept
once. The names are kept with the account's consents given in advance, under
its email address (``PK = pmc#<address>``, ``SK = account-names``), with the
Cognito sub of the account they belong to, so they count only while that
account holds the address. Each name of at least :data:`MIN_SEARCH_LENGTH`
characters is also written to a search entry (``PK = acn#<first three
characters>``) that the participant list's account search reads by prefix.

The pure rules come first, then the reads and writes, which take a low-level
DynamoDB client so every function that keeps or reads these names does so
alike. See docs/meeting-consent.md#the-match-is-by-display-name.
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .rules import normalize_name

MAX_DISPLAY_NAME_LENGTH = 100
MAX_SHORT_NAMES = 5
MAX_SHORT_NAME_LENGTH = 50
#: The fewest characters an account search takes, and the shortest name it can find.
MIN_SEARCH_LENGTH = 3
#: The longest text an account search takes.
MAX_SEARCH_LENGTH = 100
#: The most accounts one search answers.
MAX_SEARCH_RESULTS = 10

#: Which of a listed person's names an attendee's display name matched.
SOURCE_LIST = "list"
SOURCE_DISPLAY_NAME = "display_name"
SOURCE_SHORT_NAME = "short_name"
NAME_SOURCES: Tuple[str, ...] = (SOURCE_LIST, SOURCE_DISPLAY_NAME, SOURCE_SHORT_NAME)

PROFILE_SK = "account-names"
INDEX_PREFIX = "acn#"
#: Written attempts at a change before a conflicting change is reported.
WRITE_ATTEMPTS = 3
BATCH_GET_MAX = 100
UNPROCESSED_RETRIES = 5

_REFUSED = re.compile(r"[<>\x00-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"\s+")


class InvalidNames(ValueError):
    """A name is malformed; the message says how."""


class NamesChanged(Exception):
    """The names changed while they were being written; read them again."""


class NamesNotSaved(NamesChanged):
    """The names could not be written for a reason other than a conflicting change."""


# ── the rules ───────────────────────────────────────────────────────────────


def clean_name(value: Any, limit: int, what: str) -> str:
    """One name trimmed, with its inner spacing collapsed; "" when empty.

    Raises :class:`InvalidNames` for a value that is not text, holds a control
    character, ``<`` or ``>``, or is longer than ``limit`` once trimmed.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidNames(f"{what} must be text")
    if _REFUSED.search(value):
        raise InvalidNames(f"{what} cannot contain control characters, < or >")
    name = _SPACES.sub(" ", unicodedata.normalize("NFC", value)).strip()
    if len(name) > limit:
        raise InvalidNames(f"{what} must be at most {limit} characters")
    return name


def clean_names(display_name: Any, short_names: Any) -> Tuple[str, List[str]]:
    """The display name and short names as they are kept.

    A short name that repeats another, or the display name, compared ignoring
    case and spacing, is kept once; more than :data:`MAX_SHORT_NAMES` different
    short names are refused.
    """
    display = clean_name(display_name, MAX_DISPLAY_NAME_LENGTH, "The display name")
    if short_names is None:
        short_names = []
    if not isinstance(short_names, (list, tuple)):
        raise InvalidNames("Short names must be a list")
    seen = {normalize_name(display)} if display else set()
    shorts: List[str] = []
    for value in short_names:
        name = clean_name(value, MAX_SHORT_NAME_LENGTH, "A short name")
        key = normalize_name(name)
        if name and key not in seen:
            seen.add(key)
            shorts.append(name)
    if len(shorts) > MAX_SHORT_NAMES:
        raise InvalidNames(f"At most {MAX_SHORT_NAMES} short names")
    return display, shorts


def search_text(value: Any) -> str:
    """An account search's text as it is compared: normalised as names are.

    Raises :class:`InvalidNames` when it is shorter than
    :data:`MIN_SEARCH_LENGTH` or longer than :data:`MAX_SEARCH_LENGTH`
    characters, or holds a character a name or a user pool filter cannot.
    """
    if not isinstance(value, str):
        raise InvalidNames("Type at least 3 characters")
    if _REFUSED.search(value) or '"' in value or "\\" in value:
        raise InvalidNames("The search cannot contain control characters, quotes, <, > or \\")
    text = normalize_name(value)
    if len(text) < MIN_SEARCH_LENGTH:
        raise InvalidNames(f"Type at least {MIN_SEARCH_LENGTH} characters")
    if len(text) > MAX_SEARCH_LENGTH:
        raise InvalidNames(f"The search must be at most {MAX_SEARCH_LENGTH} characters")
    return text


def profile_key(email: str) -> Dict[str, Dict[str, str]]:
    """The names' key: with the address's consents given in advance."""
    return {"PK": {"S": f"pmc#{email.strip().casefold()}"}, "SK": {"S": PROFILE_SK}}


def index_entries(email: str, display_name: str, short_names: Sequence[str]) -> List[Dict]:
    """The search entries for an account's names, as DynamoDB items.

    One per name of at least :data:`MIN_SEARCH_LENGTH` characters, keyed by
    its first three normalised characters and then the whole name and the
    address, so a prefix of at least three characters reads one partition.
    Each carries the display name, which is what the search shows.
    """
    address = email.strip().casefold()
    entries: Dict[Tuple[str, str], Dict] = {}
    for name, kind in [(display_name, SOURCE_DISPLAY_NAME)] + [
        (short, SOURCE_SHORT_NAME) for short in short_names
    ]:
        key = normalize_name(name)
        if len(key) < MIN_SEARCH_LENGTH:
            continue
        pk, sk = f"{INDEX_PREFIX}{key[:MIN_SEARCH_LENGTH]}", f"{key}\t{address}"
        entries.setdefault(
            (pk, sk),
            {
                "PK": {"S": pk},
                "SK": {"S": sk},
                "Email": {"S": address},
                "Kind": {"S": kind},
                "DisplayName": {"S": display_name or ""},
            },
        )
    return list(entries.values())


def account_names(
    profile: Optional[Mapping[str, Any]], account: Optional[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The names an account goes by, or None when the address has no account.

    ``account`` is the user pool's answer for the address (``sub`` and
    ``name``); ``profile`` the names kept for it, which count only when they
    were kept for that account (the same ``sub``). The display name is the
    profile's, or the account's ``name`` when the profile has none.
    """
    if not account or not account.get("sub"):
        return None
    mine = profile if profile and profile.get("Subject") == account.get("sub") else {}
    display = str(mine.get("DisplayName") or "").strip()
    return {
        "display_name": display or str(account.get("name") or "").strip(),
        "short_names": [str(s) for s in mine.get("ShortNames") or [] if isinstance(s, str)],
    }


# ── reads and writes ────────────────────────────────────────────────────────


def _profile_out(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "DisplayName": (item.get("DisplayName") or {}).get("S") or "",
        "ShortNames": [
            v.get("S") for v in (item.get("ShortNames") or {}).get("L") or [] if v.get("S")
        ],
        "Subject": (item.get("Subject") or {}).get("S") or "",
        "UpdatedAt": (item.get("UpdatedAt") or {}).get("S") or "",
    }


def read_profile(client: Any, table: str, email: str) -> Optional[Dict[str, Any]]:
    """The names kept for one address, or None."""
    item = client.get_item(TableName=table, Key=profile_key(email), ConsistentRead=True).get("Item")
    return _profile_out(item) if item else None


def read_profiles(client: Any, table: str, emails: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """The names kept for each address that has some, by the address in lower case."""
    unique = sorted({e.strip().casefold() for e in emails if isinstance(e, str) and e.strip()})
    found: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(unique), BATCH_GET_MAX):
        keys = [profile_key(email) for email in unique[start : start + BATCH_GET_MAX]]
        request = {table: {"Keys": keys}}
        attempt = 0
        while request:
            response = client.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(table, []):
                found[item["PK"]["S"][len("pmc#") :]] = _profile_out(item)
            request = response.get("UnprocessedKeys") or {}
            attempt += 1
            if request and attempt > UNPROCESSED_RETRIES:
                raise NamesChanged("the names could not all be read; try again")
            if request:
                time.sleep(0.05 * 2**attempt)
    return found


def save_profile(
    client: Any,
    table: str,
    *,
    email: str,
    display_name: Any,
    short_names: Any,
    subject: str,
    updated_by: str,
    now: str,
) -> Dict[str, Any]:
    """Keep an account's names, replacing the ones kept before, with their search entries.

    ``subject`` is the Cognito sub of the account that holds ``email``. Names
    kept for another account with that address are replaced. With no name
    left, the names and their search entries are removed. One transaction,
    conditional on the names being as read, retried a few times.
    """
    display, shorts = clean_names(display_name, short_names)
    address = email.strip().casefold()
    key = profile_key(address)
    for _attempt in range(WRITE_ATTEMPTS):
        before = client.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
        old = _profile_out(before) if before else None
        if before:
            condition = {
                "ConditionExpression": "UpdatedAt = :prev",
                "ExpressionAttributeValues": {":prev": {"S": old["UpdatedAt"]}},
            }
        else:
            condition = {"ConditionExpression": "attribute_not_exists(PK)"}
        new_entries = index_entries(address, display, shorts)
        new_keys = {(e["PK"]["S"], e["SK"]["S"]) for e in new_entries}
        stale = []
        if old:
            for entry in index_entries(address, old["DisplayName"], old["ShortNames"]):
                if (entry["PK"]["S"], entry["SK"]["S"]) not in new_keys:
                    stale.append({"PK": entry["PK"], "SK": entry["SK"]})
        items: List[Dict[str, Any]] = []
        if display or shorts:
            profile = {
                **key,
                "Email": {"S": address},
                "Subject": {"S": subject},
                "DisplayName": {"S": display},
                "ShortNames": {"L": [{"S": s} for s in shorts]},
                "UpdatedAt": {"S": now},
                "UpdatedBy": {"S": updated_by[:256]},
            }
            items.append({"Put": {"TableName": table, "Item": profile, **condition}})
        elif before:
            items.append({"Delete": {"TableName": table, "Key": key, **condition}})
        items += [{"Delete": {"TableName": table, "Key": k}} for k in stale]
        items += [{"Put": {"TableName": table, "Item": e}} for e in new_entries]
        if not items:
            return {"DisplayName": "", "ShortNames": [], "Subject": subject, "UpdatedAt": ""}
        try:
            client.transact_write_items(TransactItems=items)
        except client.exceptions.TransactionCanceledException as error:
            reasons = getattr(error, "response", {}).get("CancellationReasons") or []
            retried = ("ConditionalCheckFailed", "TransactionConflict")
            if reasons and not any(r.get("Code") in retried for r in reasons):
                # Cancelled for another reason (throttling, for one): not a conflict.
                raise NamesNotSaved("the names could not be saved; try again") from error
            continue
        return {"DisplayName": display, "ShortNames": shorts, "Subject": subject, "UpdatedAt": now}
    raise NamesChanged("the names were changed at the same time; read them again")


def delete_profile(client: Any, table: str, email: str) -> None:
    """Remove the names kept for an address and their search entries."""
    address = email.strip().casefold()
    before = client.get_item(TableName=table, Key=profile_key(address), ConsistentRead=True).get(
        "Item"
    )
    if not before:
        return
    old = _profile_out(before)
    for entry in index_entries(address, old["DisplayName"], old["ShortNames"]):
        client.delete_item(TableName=table, Key={"PK": entry["PK"], "SK": entry["SK"]})
    client.delete_item(TableName=table, Key=profile_key(address))


def search_index(client: Any, table: str, text: str, limit: int) -> List[Dict[str, str]]:
    """Accounts with a display name or short name starting with ``text``.

    ``text`` is a :func:`search_text` answer. Answers at most ``limit``
    different addresses, each with the display name kept with it, in the
    order of the names matched.
    """
    response = client.query(
        TableName=table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": f"{INDEX_PREFIX}{text[:MIN_SEARCH_LENGTH]}"},
            ":prefix": {"S": text},
        },
        Limit=limit * (MAX_SHORT_NAMES + 1),
    )
    out: Dict[str, Dict[str, str]] = {}
    for item in response.get("Items") or []:
        email = (item.get("Email") or {}).get("S") or ""
        if email and email not in out:
            out[email] = {"email": email, "name": (item.get("DisplayName") or {}).get("S") or ""}
        if len(out) >= limit:
            break
    return list(out.values())
