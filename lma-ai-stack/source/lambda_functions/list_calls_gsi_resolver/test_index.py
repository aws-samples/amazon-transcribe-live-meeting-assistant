# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the AppSync resolver behind the meeting list.

``listCallsDateRange`` and ``getCallCount`` render the first screen every user
sees, so the behaviours pinned here are the ones whose failure is immediately
visible: the set of meetings a caller is entitled to see, that following
``nextToken`` to exhaustion yields each meeting exactly once, that the date
range includes its own endpoints, newest-first ordering, page-size clamping,
and the page caps that bound server latency.

The GSI query is driven through a small in-memory stand-in for a DynamoDB
table which *evaluates* the real ``KeyConditionExpression`` the module builds
and paginates the way DynamoDB does (sort by sort key, ``Limit`` items per
page, resume after ``ExclusiveStartKey``). Asserting against recorded call
arguments alone would not catch a range bound that silently excludes rows or a
resume key that skips them, which is the class of defect that matters here.

No AWS access: the table resource and the BatchGetItem client are both
replaced, and the module is imported with a region already in the environment
because it builds a boto3 resource at import time.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

TABLE_NAME = "lma-event-sourcing-table"

# index.py builds a boto3 resource and reads LOG_LEVEL at import time, and
# resource construction resolves an endpoint even though every call below is
# stubbed. Without a region in the environment the IMPORT itself raises
# NoRegionError, so these are set before the module is loaded.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE_NAME", TABLE_NAME)

HERE = Path(__file__).resolve().parent


def _load_module(name: str, path: Path):
    """Import a module from an explicit path.

    Every Lambda source directory in this tree has its own ``index.py``, so a
    plain ``import index`` would resolve to whichever directory happens to
    come first on sys.path.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


index = _load_module("list_calls_gsi_resolver_index", HERE / "index.py")


# --------------------------------------------------------------------------
# In-memory DynamoDB stand-ins
# --------------------------------------------------------------------------


def _matches(condition, item) -> bool:
    """Evaluate a boto3 key condition against a plain dict.

    Only the operators the resolver can build are implemented; anything else
    fails loudly rather than silently matching, so a future change to the key
    condition cannot quietly turn these tests into no-ops.
    """
    expression = condition.get_expression()
    operator = expression["operator"]
    values = expression["values"]
    if operator == "AND":
        return all(_matches(value, item) for value in values)
    actual = item.get(values[0].name)
    if operator == "=":
        return actual == values[1]
    if operator == "BETWEEN":
        return values[1] <= actual <= values[2]
    if operator == ">=":
        return actual >= values[1]
    if operator == "<=":
        return actual <= values[1]
    raise AssertionError(f"stand-in table does not implement operator {operator!r}")


class FakeTable:
    """A DynamoDB ``Table`` whose ``query`` serves one GSI from memory."""

    def __init__(self, items, error=None, page_size=None):
        self.items = list(items)
        self.table_name = TABLE_NAME
        self.error = error
        # Used only by the count paths, which send no Limit of their own; real
        # DynamoDB pages them at 1 MB.
        self.page_size = page_size
        self.queries = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.error is not None:
            raise self.error
        assert kwargs["IndexName"] == index.TYPE_DATE_INDEX
        matched = [i for i in self.items if _matches(kwargs["KeyConditionExpression"], i)]
        matched.sort(key=lambda i: i["SK"], reverse=not kwargs.get("ScanIndexForward", True))

        start = 0
        start_key = kwargs.get("ExclusiveStartKey")
        if start_key:
            coordinates = [(i["PK"], i["SK"]) for i in matched]
            # A resume key that names no row would mean the token did not come
            # from this index; DynamoDB would reject it.
            start = coordinates.index((start_key["PK"], start_key["SK"])) + 1

        limit = kwargs.get("Limit") or self.page_size or len(matched)
        page = matched[start : start + limit]

        response = {"Count": len(page), "ScannedCount": len(page)}
        if kwargs.get("Select") != "COUNT":
            response["Items"] = page
        if page and start + limit < len(matched):
            last = page[-1]
            response["LastEvaluatedKey"] = {
                "PK": last["PK"],
                "SK": last["SK"],
                "ItemType": last["ItemType"],
            }
        return response


class FakeResource:
    """Stands in for ``boto3.resource("dynamodb")``."""

    def __init__(self, table):
        self._table = table

    def Table(self, name):  # noqa: N802 - boto3 resource API spelling
        assert name == TABLE_NAME, f"unexpected table {name!r}"
        return self._table


class FakeBatchClient:
    """A DynamoDB client implementing just ``batch_get_item``."""

    def __init__(self, details=(), defer_first=0):
        self.details = {item["PK"]: item for item in details}
        # Number of keys withheld from the first response, to exercise the
        # UnprocessedKeys retry that DynamoDB forces under throttling.
        self.defer_first = defer_first
        self.requests = []

    def batch_get_item(self, RequestItems):  # noqa: N803 - boto3 kwarg spelling
        keys = list(RequestItems[TABLE_NAME]["Keys"])
        self.requests.append(keys)
        assert len(keys) <= 100, "BatchGetItem accepts at most 100 keys per call"
        withheld = []
        if self.defer_first:
            withheld, keys = keys[: self.defer_first], keys[self.defer_first :]
            self.defer_first = 0
        response = {
            "Responses": {
                TABLE_NAME: [self.details[k["PK"]] for k in keys if k["PK"] in self.details]
            }
        }
        if withheld:
            response["UnprocessedKeys"] = {TABLE_NAME: {"Keys": withheld}}
        return response


@pytest.fixture(name="install")
def install_fixture(monkeypatch):
    """Return a callable that swaps both DynamoDB entry points for fakes.

    The resolver reaches DynamoDB two ways: the module-level resource for the
    GSI query, and a freshly constructed ``boto3.resource(...).meta.client``
    for BatchGetItem. Both have to be replaced or a test would touch AWS.
    """

    def _install(list_items=(), details=(), error=None, page_size=None, defer_first=0):
        table = FakeTable(list_items, error=error, page_size=page_size)
        client = FakeBatchClient(details, defer_first=defer_first)
        monkeypatch.setattr(index, "dynamodb", FakeResource(table))
        monkeypatch.setattr(
            index,
            "boto3",
            SimpleNamespace(
                resource=lambda _name: SimpleNamespace(meta=SimpleNamespace(client=client))
            ),
        )
        return SimpleNamespace(table=table, client=client)

    return _install


# --------------------------------------------------------------------------
# Fixture data helpers
# --------------------------------------------------------------------------

DAY = "2026-05-27"
RANGE_START = f"{DAY}T00:00:00.000Z"
RANGE_END = f"{DAY}T23:59:59.999Z"
BOB = "bob@example.com"
ALICE = "alice@example.com"


def list_row(call_id, iso, shard=0, **extra):
    """Build a list-tracking row as the TypeDateIndex projects it."""
    return {
        "PK": f"cls#{iso[:10]}#s#{shard}",
        "SK": f"ts#{iso}#id#{call_id}",
        "ItemType": index.ITEM_TYPE_CALL,
        "CallId": call_id,
        "CreatedAt": iso,
        **extra,
    }


def detail_row(call_id, owner=BOB, **extra):
    """Build the ``c#<CallId>`` call-detail row BatchGetItem returns."""
    return {
        "PK": f"c#{call_id}",
        "SK": f"c#{call_id}",
        "CallId": call_id,
        "Owner": owner,
        **extra,
    }


def day_of_calls(count, owners=None, day=DAY):
    """``count`` list rows one minute apart, oldest first, with details."""
    owners = owners or {}
    rows, details = [], []
    for i in range(count):
        call_id = f"call-{i:03d}"
        rows.append(list_row(call_id, f"{day}T10:{i:02d}:00.000Z"))
        details.append(detail_row(call_id, owner=owners.get(call_id, BOB)))
    return rows, details


def event(field="listCallsDateRange", start=RANGE_START, end=RANGE_END, **args):
    """Build an AppSync resolver event for a Cognito User Pools caller."""
    username = args.pop("username", BOB)
    email = args.pop("email", None)
    groups = args.pop("groups", None)
    claims = {"cognito:username": username}
    if email is not None:
        claims["email"] = email
    if groups is not None:
        claims["cognito:groups"] = groups
    arguments = {"startDateTime": start, "endDateTime": end}
    arguments.update({k: v for k, v in args.items() if v is not None})
    return {
        "info": {"fieldName": field},
        "arguments": arguments,
        "identity": {"username": username, "claims": claims},
    }


def page_through(max_pages=50, **event_args):
    """Follow nextToken to exhaustion the way the UI's loader does."""
    seen, token = [], None
    for _ in range(max_pages):
        result = index.handler(event(nextToken=token, **event_args), None)
        seen.extend(call["CallId"] for call in result["Calls"])
        token = result["nextToken"]
        if not token:
            return seen
    raise AssertionError("pagination did not terminate")


# --------------------------------------------------------------------------
# nextToken encoding
# --------------------------------------------------------------------------


def test_next_token_round_trips_the_resume_key() -> None:
    key = {
        "PK": "cls#2026-05-27#s#3",
        "SK": "ts#2026-05-27T10:00:00.000Z#id#abc",
        "ItemType": "call",
    }
    assert index._decode_token(index._encode_token(key)) == key


def test_next_token_uses_the_url_safe_alphabet() -> None:
    """The token travels as a GraphQL string and can end up in a query string.

    Standard base64 emits "+" and "/", which a URL round-trip rewrites into
    something the decoder no longer accepts. The key here ends in "~" because
    that is one of the few byte values whose standard-base64 output leaves the
    URL-safe alphabet at all, so the assertion can tell the two encoders apart.
    """
    key = {"SK": "ts#2026-05-27T10:00:00.000Z#id#~~~"}
    token = index._encode_token(key)
    assert "+" not in token and "/" not in token
    assert index._decode_token(token) == key


def test_a_numeric_resume_key_survives_the_round_trip() -> None:
    """DynamoDB returns numbers as Decimal, which plain json cannot encode.

    An unencodable resume key would raise inside the resolver and take the
    whole meeting list down at the page boundary rather than just the token.
    """
    key = {"Shard": Decimal("3"), "Score": Decimal("1.5")}
    decoded = index._decode_token(index._encode_token(key))
    assert decoded == {"Shard": 3, "Score": 1.5}
    # An integral key must stay integral: it is a key value, not a measurement.
    assert isinstance(decoded["Shard"], int)
    assert isinstance(decoded["Score"], float)


def test_an_empty_resume_key_produces_no_token() -> None:
    assert index._encode_token(None) is None
    assert index._encode_token({}) is None
    assert index._decode_token(None) is None


def test_a_malformed_next_token_serves_the_first_page(install) -> None:
    """A stale or truncated token restarts the listing instead of erroring."""
    rows, details = day_of_calls(3)
    env = install(rows, details)
    result = index.handler(event(nextToken="not-a-real-token", limit=10), None)
    assert [c["CallId"] for c in result["Calls"]] == ["call-002", "call-001", "call-000"]
    assert "ExclusiveStartKey" not in env.table.queries[0]


# --------------------------------------------------------------------------
# Pagination integrity
# --------------------------------------------------------------------------


def test_paging_returns_every_meeting_exactly_once(install) -> None:
    """Three pages of five over twelve meetings: no gaps, no repeats."""
    rows, details = day_of_calls(12)
    install(rows, details)
    seen = page_through(limit=5)
    assert seen == [f"call-{i:03d}" for i in range(11, -1, -1)]
    assert len(set(seen)) == len(seen)


def test_the_resume_key_continues_after_the_last_returned_meeting(install) -> None:
    rows, details = day_of_calls(6)
    env = install(rows, details)

    first = index.handler(event(limit=2), None)
    assert [c["CallId"] for c in first["Calls"]] == ["call-005", "call-004"]

    second = index.handler(event(limit=2, nextToken=first["nextToken"]), None)
    assert [c["CallId"] for c in second["Calls"]] == ["call-003", "call-002"]
    resume = env.table.queries[1]["ExclusiveStartKey"]
    assert resume["SK"] == f"ts#{DAY}T10:04:00.000Z#id#call-004"


def test_the_last_page_ends_the_listing(install) -> None:
    """No token on the final page, or the UI's loader would spin."""
    rows, details = day_of_calls(4)
    install(rows, details)
    result = index.handler(event(limit=4), None)
    assert len(result["Calls"]) == 4
    assert result["nextToken"] is None


def test_a_page_shorter_than_the_limit_ends_the_listing(install) -> None:
    rows, details = day_of_calls(3)
    install(rows, details)
    result = index.handler(event(limit=10), None)
    assert len(result["Calls"]) == 3
    assert result["nextToken"] is None


def test_a_full_page_with_more_to_come_carries_a_token(install) -> None:
    rows, details = day_of_calls(7)
    install(rows, details)
    result = index.handler(event(limit=3), None)
    assert len(result["Calls"]) == 3
    assert result["nextToken"] is not None
    assert index._decode_token(result["nextToken"])["SK"].endswith("#id#call-004")


@pytest.mark.xfail(
    strict=True,
    reason="known defect: the resume key advances past rows dropped by the "
    "entitlement filter once the page is full, so those meetings are never "
    "returned on any page (see report)",
)
def test_paging_returns_every_meeting_exactly_once_when_some_are_filtered(install) -> None:
    """Paging must be complete for a caller who shares the range with others.

    A caller whose page contains other people's meetings gets a short first
    page; once a later page fills the limit mid-way, the rows after the cut
    must still be reachable through the token.
    """
    rows, details = day_of_calls(9, owners={"call-007": ALICE})
    install(rows, details)
    seen = page_through(limit=3)
    expected = [f"call-{i:03d}" for i in range(8, -1, -1) if i != 7]
    assert seen == expected


def test_the_listing_stops_after_the_page_cap_and_hands_back_a_token(install) -> None:
    """A caller entitled to little of a busy range must not stall the API.

    The cap bounds server latency; returning the token keeps the listing
    resumable rather than silently ending it.
    """
    rows, details = day_of_calls(40, owners={f"call-{i:03d}": ALICE for i in range(40)})
    env = install(rows, details)
    result = index.handler(event(limit=1), None)
    assert result["Calls"] == []
    assert result["nextToken"] is not None
    # The cap is a local in list_calls_date_range, so it is pinned by count:
    # ten index queries per client request, no matter how little is visible.
    assert len(env.table.queries) == 10


# --------------------------------------------------------------------------
# Date range and shard handling
# --------------------------------------------------------------------------


def test_range_bounds_are_derived_from_the_timestamp_prefix() -> None:
    """The sort key is ``ts#<ISO8601>#id#<CallId>``; the bounds must match it.

    The upper bound appends a separator plus "~" so a row stamped at exactly
    the end of the range still sorts below it.
    """
    expression = index._build_key_condition(RANGE_START, RANGE_END).get_expression()
    item_type, between = expression["values"]
    assert item_type.get_expression()["values"][1] == "call"
    between_expression = between.get_expression()
    assert between_expression["operator"] == "BETWEEN"
    assert between_expression["values"][1] == f"ts#{RANGE_START}"
    assert between_expression["values"][2] == f"ts#{RANGE_END}#~"


@pytest.mark.parametrize(
    ("start", "end", "operator", "bounds"),
    [
        (RANGE_START, None, ">=", (f"ts#{RANGE_START}",)),
        (None, RANGE_END, "<=", (f"ts#{RANGE_END}#~",)),
    ],
    ids=["from-only", "until-only"],
)
def test_a_half_open_range_bounds_only_the_side_it_names(start, end, operator, bounds) -> None:
    expression = index._build_key_condition(start, end).get_expression()
    # ItemType equality AND a one-sided sort-key bound.
    assert expression["operator"] == "AND"
    sort_key = expression["values"][1].get_expression()
    assert sort_key["operator"] == operator
    assert sort_key["values"][1:] == bounds


def test_an_unbounded_range_still_restricts_the_query_to_calls() -> None:
    """Without a range the query must stay on ItemType=call, never a scan."""
    expression = index._build_key_condition(None, None).get_expression()
    assert expression["operator"] == "="
    assert expression["values"][0].name == "ItemType"
    assert expression["values"][1] == index.ITEM_TYPE_CALL


def test_a_meeting_at_each_end_of_the_range_is_included(install) -> None:
    """Inclusive endpoints: "today" must show a meeting starting at 00:00:00.

    The upper endpoint is the one at risk, since the sort key continues past
    the timestamp with ``#id#<CallId>``.
    """
    rows = [list_row("first", RANGE_START), list_row("last", RANGE_END)]
    install(rows, [detail_row("first"), detail_row("last")])
    result = index.handler(event(limit=10), None)
    assert [c["CallId"] for c in result["Calls"]] == ["last", "first"]


def test_meetings_outside_the_range_are_excluded(install) -> None:
    rows = [
        list_row("too-early", f"{DAY}T00:00:00.000Z"),
        list_row("inside", f"{DAY}T12:00:00.000Z"),
        list_row("too-late", f"{DAY}T18:00:00.001Z"),
    ]
    install(rows, [detail_row("too-early"), detail_row("inside"), detail_row("too-late")])
    result = index.handler(
        event(start=f"{DAY}T06:00:00.000Z", end=f"{DAY}T18:00:00.000Z", limit=10), None
    )
    assert [c["CallId"] for c in result["Calls"]] == ["inside"]


def test_a_range_spanning_a_day_boundary_returns_both_days(install) -> None:
    """List rows are sharded per day, so a multi-day range spans partitions.

    The index query is keyed on ItemType rather than the shard, so one query
    has to cover them all; per-shard fan-out would drop a day.
    """
    rows = [
        list_row("yesterday-late", "2026-05-26T23:30:00.000Z", shard=3),
        list_row("today-early", "2026-05-27T00:30:00.000Z", shard=0),
        list_row("today-late", "2026-05-27T22:00:00.000Z", shard=7),
    ]
    details = [detail_row(r["CallId"]) for r in rows]
    env = install(rows, details)
    result = index.handler(
        event(start="2026-05-26T00:00:00.000Z", end="2026-05-27T23:59:59.999Z", limit=10), None
    )
    assert [c["CallId"] for c in result["Calls"]] == [
        "today-late",
        "today-early",
        "yesterday-late",
    ]
    # One index query, not one per shard.
    assert len(env.table.queries) == 1


# --------------------------------------------------------------------------
# Ordering, page size and empty results
# --------------------------------------------------------------------------


def test_meetings_are_returned_newest_first(install) -> None:
    rows, details = day_of_calls(5)
    env = install(rows, details)
    result = index.handler(event(limit=5), None)
    assert env.table.queries[0]["ScanIndexForward"] is False
    timestamps = [c["CreatedAt"] for c in result["Calls"]]
    assert timestamps == sorted(timestamps, reverse=True)
    assert timestamps[0] == f"{DAY}T10:04:00.000Z"


def test_an_absent_limit_uses_the_default_page_size(install) -> None:
    rows, details = day_of_calls(60)
    env = install(rows, details)
    result = index.handler(event(), None)
    assert env.table.queries[0]["Limit"] == index.DEFAULT_PAGE_SIZE
    assert len(result["Calls"]) == index.DEFAULT_PAGE_SIZE


def test_an_oversized_limit_is_clamped(install) -> None:
    """A client asking for thousands of rows must not force an oversized read."""
    rows, details = day_of_calls(250)
    env = install(rows, details)
    result = index.handler(event(limit=5000), None)
    assert env.table.queries[0]["Limit"] == index.MAX_PAGE_SIZE
    assert len(result["Calls"]) == index.MAX_PAGE_SIZE
    assert index.MAX_PAGE_SIZE == 200


def test_a_range_with_no_meetings_returns_an_empty_list(install) -> None:
    """An empty result is an empty list and no token, not an error."""
    env = install([], [])
    result = index.handler(event(limit=10), None)
    assert result == {"Calls": [], "nextToken": None}
    # An empty page with nothing beyond it ends the loop at once rather than
    # re-querying up to the page cap, and there is nothing to enrich.
    assert len(env.table.queries) == 1
    assert env.client.requests == []


# --------------------------------------------------------------------------
# Entitlement filtering
# --------------------------------------------------------------------------


def test_the_owner_sees_their_own_meeting(install) -> None:
    install([list_row("mine", RANGE_END)], [detail_row("mine", owner=BOB)])
    result = index.handler(event(limit=10), None)
    assert [c["CallId"] for c in result["Calls"]] == ["mine"]


def test_a_user_does_not_see_another_users_meetings(install) -> None:
    install([list_row("theirs", RANGE_END)], [detail_row("theirs", owner=ALICE)])
    result = index.handler(event(limit=10), None)
    assert result["Calls"] == []


@pytest.mark.parametrize(
    "shared_with",
    [BOB, f"carol@example.com, {BOB}", [ALICE, BOB], ["", BOB]],
    ids=["single-string", "comma-separated", "list", "list-with-blank"],
)
def test_a_meeting_shared_with_the_caller_is_visible(install, shared_with) -> None:
    """SharedWith is stored as a list now and as a comma string on older rows."""
    install(
        [list_row("shared", RANGE_END)],
        [detail_row("shared", owner=ALICE, SharedWith=shared_with)],
    )
    result = index.handler(event(limit=10), None)
    assert [c["CallId"] for c in result["Calls"]] == ["shared"]


def test_sharing_with_someone_else_does_not_widen_visibility(install) -> None:
    install(
        [list_row("shared", RANGE_END)],
        [detail_row("shared", owner=ALICE, SharedWith=["carol@example.com"])],
    )
    assert index.handler(event(limit=10), None)["Calls"] == []


def test_the_callers_email_also_matches_ownership(install) -> None:
    """Rows written by different call paths key ownership on username or email."""
    install([list_row("mine", RANGE_END)], [detail_row("mine", owner="bob.alias@example.com")])
    result = index.handler(event(limit=10, username="bob", email="bob.alias@example.com"), None)
    assert [c["CallId"] for c in result["Calls"]] == ["mine"]


def test_an_admin_sees_every_meeting(install) -> None:
    rows, details = day_of_calls(3, owners={f"call-{i:03d}": ALICE for i in range(3)})
    install(rows, details)
    result = index.handler(event(limit=10, groups=["Admin"]), None)
    assert len(result["Calls"]) == 3


def test_an_admin_group_delivered_as_a_bare_string_is_recognised(install) -> None:
    """Some auth paths deliver cognito:groups as a scalar rather than a list."""
    install([list_row("theirs", RANGE_END)], [detail_row("theirs", owner=ALICE)])
    result = index.handler(event(limit=10, groups="Admin"), None)
    assert [c["CallId"] for c in result["Calls"]] == ["theirs"]


def test_a_non_admin_group_does_not_grant_the_admin_view(install) -> None:
    install([list_row("theirs", RANGE_END)], [detail_row("theirs", owner=ALICE)])
    assert index.handler(event(limit=10, groups=["Users", "AdminReadOnly"]), None)["Calls"] == []


def test_an_unowned_meeting_is_not_visible_to_an_unidentified_caller() -> None:
    """Blank identity must not match a blank Owner field."""
    caller = index._get_caller_identity({"identity": {}})
    assert caller == {"username": "", "email": "", "groups": [], "is_admin": False}
    assert index._call_visible_to(caller, {"Owner": "", "SharedWith": ""}) is False
    assert index._call_visible_to(caller, {}) is False


def test_the_username_claim_is_preferred_over_the_resolved_identity() -> None:
    caller = index._get_caller_identity(
        {"identity": {"username": "iam-role-session", "claims": {"cognito:username": BOB}}}
    )
    assert caller["username"] == BOB


def test_the_identity_username_is_used_when_no_claim_carries_it() -> None:
    """IAM-authed AppSync requests populate identity.username and no claims."""
    caller = index._get_caller_identity({"identity": {"username": BOB, "claims": {}}})
    assert caller["username"] == BOB


def test_the_sub_claim_is_the_final_fallback() -> None:
    caller = index._get_caller_identity({"identity": {"claims": {"sub": "uuid-1234"}}})
    assert caller["username"] == "uuid-1234"


def test_ownership_is_decided_on_the_detail_row(install) -> None:
    """Owner lives on the call-detail row; the index projection may lack it."""
    install([list_row("mine", RANGE_END)], [detail_row("mine", owner=BOB)])
    assert len(index.handler(event(limit=10), None)["Calls"]) == 1
    install([list_row("theirs", RANGE_END)], [detail_row("theirs", owner=ALICE)])
    assert index.handler(event(limit=10), None)["Calls"] == []


# --------------------------------------------------------------------------
# Detail enrichment
# --------------------------------------------------------------------------


def test_detail_attributes_enrich_the_returned_meeting(install) -> None:
    install(
        [list_row("c1", RANGE_END)],
        [detail_row("c1", Status="DONE", TotalConversationDurationMillis=Decimal("61000"))],
    )
    calls = index.handler(event(limit=10), None)["Calls"]
    assert len(calls) == 1
    call = calls[0]
    assert call.get("Status") == "DONE"
    assert call.get("TotalConversationDurationMillis") == Decimal("61000")
    # Attributes only the index projection carries survive the overlay.
    assert call["CreatedAt"] == RANGE_END


def test_the_list_row_coordinates_survive_enrichment(install) -> None:
    """The UI derives the delete / share payload from the returned PK and SK.

    The detail row carries its own PK and SK (``c#<CallId>``); if those won the
    merge, the write the UI then issues would name the same key twice and the
    Delete dialog would hang with nothing removed.
    """
    row = list_row("c1", RANGE_END, shard=2)
    install([row], [detail_row("c1")])
    call = index.handler(event(limit=10), None)["Calls"][0]
    assert call["PK"] == row["PK"] == f"cls#{DAY}#s#2"
    assert call["SK"] == row["SK"] == f"ts#{RANGE_END}#id#c1"
    assert (call.get("ListPK"), call.get("ListSK")) == (row["PK"], row["SK"])


def test_a_meeting_whose_detail_row_is_missing_is_still_listed(install) -> None:
    """A half-written meeting must not vanish from an admin's list."""
    install([list_row("orphan", RANGE_END)], [])
    result = index.handler(event(limit=10, groups=["Admin"]), None)
    assert [c["CallId"] for c in result["Calls"]] == ["orphan"]
    assert result["Calls"][0]["PK"] == f"cls#{DAY}#s#0"


def test_details_are_fetched_in_batches_within_the_service_limit(install) -> None:
    """BatchGetItem takes at most 100 keys, and a page may hold 200 rows."""
    rows, details = day_of_calls(150)
    env = install(rows, details)
    result = index.handler(event(limit=150), None)
    assert len(result["Calls"]) == 150
    assert [len(request) for request in env.client.requests] == [100, 50]


def test_details_left_unprocessed_are_retried(install) -> None:
    """DynamoDB may return keys unread; dropping them would blank out rows."""
    rows, details = day_of_calls(3)
    env = install(rows, details, defer_first=2)
    result = index.handler(event(limit=10), None)
    assert len(env.client.requests) == 2
    # Keys are requested newest-first, so the two withheld are the newest two.
    assert env.client.requests[1] == [
        {"PK": "c#call-002", "SK": "c#call-002"},
        {"PK": "c#call-001", "SK": "c#call-001"},
    ]
    assert all(call.get("Owner") == BOB for call in result["Calls"])
    assert len(result["Calls"]) == 3


# --------------------------------------------------------------------------
# Failures and routing
# --------------------------------------------------------------------------


def _client_error(code="ProvisionedThroughputExceededException"):
    return ClientError({"Error": {"Code": code, "Message": "slow down"}}, "Query")


def test_a_dynamodb_failure_reaches_the_caller_as_an_error(install) -> None:
    """The UI shows a retry message on error; a silent empty list would read
    as "you have no meetings"."""
    install([], [], error=_client_error())
    with pytest.raises(ClientError):
        index.handler(event(limit=10), None)


def test_a_dynamodb_failure_during_counting_reaches_the_caller(install) -> None:
    install([], [], error=_client_error())
    with pytest.raises(ClientError):
        index.handler(event(field="getCallCount"), None)


def test_an_unknown_field_is_rejected(install) -> None:
    install([], [])
    with pytest.raises(ValueError, match="Unknown field"):
        index.handler(event(field="deleteEverything"), None)
    with pytest.raises(ValueError, match="Unknown field"):
        index.handler({"arguments": {}}, None)


def test_the_handler_routes_both_fields(install) -> None:
    rows, details = day_of_calls(2)
    install(rows, details)
    assert "Calls" in index.handler(event(), None)
    assert set(index.handler(event(field="getCallCount"), None)) == {"count", "truncated"}


# --------------------------------------------------------------------------
# getCallCount
# --------------------------------------------------------------------------


def count_rows(owners):
    """List rows carrying Owner, which the index projection includes."""
    return [
        list_row(f"call-{i:03d}", f"{DAY}T10:{i:02d}:00.000Z", Owner=owner)
        for i, owner in enumerate(owners)
    ]


def test_an_admin_count_totals_every_page(install) -> None:
    env = install(count_rows([ALICE] * 5), page_size=2)
    result = index.handler(event(field="getCallCount", groups=["Admin"]), None)
    assert result == {"count": 5, "truncated": False}
    # COUNT reads no items, which is what makes the admin path cheap.
    assert all(query["Select"] == "COUNT" for query in env.table.queries)
    assert len(env.table.queries) == 3


def test_a_count_covers_only_the_meetings_the_caller_may_see(install) -> None:
    """The header counter must agree with the list the caller is shown."""
    env = install(count_rows([BOB, ALICE, BOB, ALICE, ALICE]), page_size=2)
    result = index.handler(event(field="getCallCount"), None)
    assert result == {"count": 2, "truncated": False}
    assert all(query["Select"] == "ALL_PROJECTED_ATTRIBUTES" for query in env.table.queries)


def test_a_count_covers_meetings_shared_with_the_caller(install) -> None:
    rows = count_rows([ALICE, ALICE])
    rows[0]["SharedWith"] = [BOB]
    rows[1]["SharedWith"] = "carol@example.com"
    install(rows)
    assert index.handler(event(field="getCallCount"), None)["count"] == 1


def test_a_count_respects_the_date_range(install) -> None:
    rows = count_rows([BOB, BOB, BOB])
    rows[0]["SK"] = "ts#2026-05-26T10:00:00.000Z#id#call-000"
    install(rows)
    result = index.handler(event(field="getCallCount"), None)
    assert result["count"] == 2


def test_an_empty_range_counts_zero(install) -> None:
    install([])
    assert index.handler(event(field="getCallCount"), None) == {"count": 0, "truncated": False}


def test_a_count_that_hits_its_page_cap_is_reported_as_truncated(install) -> None:
    """Clients render "500+" rather than a wrong total, so the flag must be set.

    One row per page is artificial; it reaches the cap without materialising
    the hundreds of thousands of rows a real 1 MB page would hold.
    """
    install(count_rows([BOB] * (index.MAX_COUNT_PAGES_NON_ADMIN + 10)), page_size=1)
    result = index.handler(event(field="getCallCount"), None)
    assert result["truncated"] is True
    assert result["count"] == index.MAX_COUNT_PAGES_NON_ADMIN


def test_an_admin_count_has_a_higher_page_cap(install) -> None:
    """COUNT pages are cheap, so admins get a larger budget before truncating."""
    assert index.MAX_COUNT_PAGES_ADMIN > index.MAX_COUNT_PAGES_NON_ADMIN
    env = install(count_rows([BOB] * (index.MAX_COUNT_PAGES_ADMIN + 5)), page_size=1)
    result = index.handler(event(field="getCallCount", groups=["Admin"]), None)
    assert result == {"count": index.MAX_COUNT_PAGES_ADMIN, "truncated": True}
    assert len(env.table.queries) == index.MAX_COUNT_PAGES_ADMIN
