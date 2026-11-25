# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The filter onAddParticipantTranscriptSegment sets, against AppSync's limits.

AppSync refuses a subscription whose enhanced filter is larger than it accepts
("Filters combination exceed maximum limit 10 for subscription."). Its limits,
from the "Exceptions" section of the AppSync extensions reference
(https://docs.aws.amazon.com/appsync/latest/devguide/extensions.html):

* a filter group holds at most 10 filters, ORed, and the `in` operator is a
  special case of OR: a filter with an `in` of n values counts as n filters;
* a filter ANDs at most 5 distinct fields;
* an `in` or `notIn` holds at most 5 values, a `containsAny` at most 20.

util.transform.toSubscriptionFilter turns the resolver's filter object into a
filter group by ANDing its top-level fields onto every branch of its `or`.

The resolver's response handler is run here under Node, with a stand-in for
@aws-appsync/utils that records the object handed to toSubscriptionFilter.
The object is expanded as toSubscriptionFilter expands it, counted as AppSync
counts it, and evaluated against published segments, to check that it stays
within the limits for any caller and sharing list and still delivers exactly
the segments of the meeting, on its transcript channels, whose sharing list
names the caller. test_shared_with_access_evaluated.py runs the same handler
through AppSync's EvaluateCode for its admission rules.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from itertools import product
from math import prod
from pathlib import Path

import pytest

AI_STACK = Path(__file__).resolve().parents[1]
RESOLVER = AI_STACK / "source" / "appsync" / "participantTranscriptSubscription.js"
SCHEMA = AI_STACK / "source" / "appsync" / "schema.graphql"
LMA_CHANNELS = (
    AI_STACK / "source" / "lambda_layers" / "configuration_layer" / "lma_channels" / "__init__.py"
)

MAX_FILTERS = 10
MAX_FIELDS_PER_FILTER = 5
MAX_LIST_VALUES = {"in": 5, "notIn": 5, "containsAny": 20}

CALLER = "bob@example.com"
CALL_ID = "c1"

UTILS_IMPORT = "import { util, extensions } from '@aws-appsync/utils';"

# Stands in for @aws-appsync/utils: records the filter object the handler
# builds, and turns any refusal into an exception.
STAND_IN = """
class Refused extends Error {}
const recorded = [];
const util = {
  unauthorized() { throw new Refused('Unauthorized'); },
  error(message) { throw new Refused(message); },
  dynamodb: { toMapValues: (value) => value },
  transform: { toSubscriptionFilter: (filter) => ({ input: filter }) },
};
const extensions = { setSubscriptionFilter: (filter) => recorded.push(filter.input) };
"""

HARNESS = """
const ctx = JSON.parse(process.argv[2]);
try {
  response(ctx);
  process.stdout.write(JSON.stringify({ filters: recorded }));
} catch (e) {
  if (!(e instanceof Refused)) throw e;
  process.stdout.write(JSON.stringify({ refused: e.message }));
}
"""


def _load_lma_channels():
    spec = importlib.util.spec_from_file_location("lma_channels_for_filter_test", LMA_CHANNELS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def handler(tmp_path_factory) -> Path:
    if shutil.which("node") is None:
        pytest.skip("needs Node.js to run the resolver")
    source = RESOLVER.read_text(encoding="utf-8")
    assert UTILS_IMPORT in source
    script = tmp_path_factory.mktemp("participant_filter") / "handler.mjs"
    script.write_text(source.replace(UTILS_IMPORT, STAND_IN) + HARNESS, encoding="utf-8")
    return script


def _subscribe(handler: Path, shared_with, username: str = CALLER) -> dict:
    ctx = {
        "args": {"CallId": CALL_ID},
        "identity": {"username": username, "groups": ["Participant"]},
        "result": {"PK": f"c#{CALL_ID}", "SK": f"c#{CALL_ID}", "SharedWith": shared_with},
        "info": {"selectionSetList": ["CallId", "SegmentId", "Transcript", "Channel"]},
    }
    run = subprocess.run(
        ["node", str(handler), json.dumps(ctx)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(run.stdout)


def _filter_for(handler: Path, shared_with, username: str = CALLER) -> dict:
    outcome = _subscribe(handler, shared_with, username)
    assert "refused" not in outcome, outcome
    (only,) = outcome["filters"]
    return only


# ── toSubscriptionFilter's expansion and AppSync's count ───────────────────


def _field_alternatives(field: str, condition: dict) -> list[list[tuple]]:
    # Two or more operators on one field are ORed.
    return [[(field, op, value)] for op, value in condition.items()]


def _expand(node: dict) -> list[list[tuple]]:
    """The filter group toSubscriptionFilter makes of a filter object: a list of
    filters (ORed), each a list of (field, operator, value) rules (ANDed)."""
    conjuncts: list[list[list[tuple]]] = []
    for key, value in node.items():
        if key == "and":
            for child in value:
                conjuncts.append(_expand(child))
        elif key == "or":
            conjuncts.append([rules for child in value for rules in _expand(child)])
        else:
            conjuncts.append(_field_alternatives(key, value))
    return [[rule for part in choice for rule in part] for choice in product(*conjuncts)]


def _combinations(group: list[list[tuple]]) -> int:
    """The filters AppSync counts: each `in` of n values counts as n."""
    return sum(prod(len(value) if op == "in" else 1 for _, op, value in rules) for rules in group)


def _assert_within_limits(filter_object: dict) -> None:
    group = _expand(filter_object)
    assert _combinations(group) <= MAX_FILTERS, (_combinations(group), group)
    for rules in group:
        assert len({field for field, _, _ in rules}) <= MAX_FIELDS_PER_FILTER, rules
        for _, op, value in rules:
            if op in MAX_LIST_VALUES:
                assert len(value) <= MAX_LIST_VALUES[op], rules


def test_the_count_multiplies_each_filter_by_the_values_of_its_in() -> None:
    # The documented example: one filter with an `in` of two values is two.
    documented = {"userId": {"eq": 1}, "group": {"in": ["Admin", "Developer"]}}
    assert _combinations(_expand(documented)) == 2
    # Four sharing-list alternatives, each with an `in` of five channels, are
    # twenty, as AppSync counts them when it refuses such a filter.
    four_by_five = {
        "CallId": {"eq": CALL_ID},
        "Channel": {"in": ["REMOTE", "LOCAL", "CALLER", "AGENT", "MEETING_CHAT"]},
        "or": [{"SharedWith": {op: "x"}} for op in ("eq", "beginsWith", "contains", "notContains")],
    }
    assert _combinations(_expand(four_by_five)) == 20


# ── the resolver's filter ──────────────────────────────────────────────────

OTHERS = [f"person{n}@example.com" for n in range(60)]
SHARING_LISTS = {
    "only the caller": [CALLER],
    "caller first": [CALLER, OTHERS[0]],
    "caller last": [OTHERS[0], CALLER],
    "caller among many": OTHERS[:30] + [CALLER] + OTHERS[30:],
    "bracketed string": f"[{OTHERS[0]}, {CALLER}]",
}


@pytest.mark.parametrize("shape", list(SHARING_LISTS))
def test_the_filter_is_within_appsyncs_limits(handler, shape) -> None:
    _assert_within_limits(_filter_for(handler, SHARING_LISTS[shape]))


@pytest.mark.parametrize(
    "username", [CALLER, "a@b.co", "someone.with.a.much.longer.name+tag@example.org"]
)
def test_the_filter_does_not_grow_with_the_caller_or_the_list(handler, username) -> None:
    sizes = {
        _combinations(_expand(_filter_for(handler, OTHERS[:n] + [username], username)))
        for n in (0, 5, 60)
    }
    assert len(sizes) == 1, sizes


def _matches(rule: tuple, message: dict) -> bool:
    field, op, operand = rule
    value = message.get(field)
    if value is None:
        return False
    if op == "eq":
        return value == operand
    if op == "beginsWith":
        return isinstance(value, str) and value.startswith(operand)
    if op == "contains":
        return isinstance(value, str) and operand in value
    if op == "in":
        return value in operand
    if op == "notIn":
        return value not in operand
    raise AssertionError(f"operator not modelled here: {op}")


def _delivered(filter_object: dict, message: dict) -> bool:
    return any(all(_matches(rule, message) for rule in rules) for rules in _expand(filter_object))


def _channel_enum() -> list[str]:
    body = re.search(
        r"^enum Channel \{(.*?)^\}", SCHEMA.read_text(encoding="utf-8"), re.S | re.M
    ).group(1)
    return [
        line.split()[0]
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _published(entries: list[str]) -> str:
    # The form dDbPutCondition.response.vtl publishes SharedWith in.
    return "[" + ", ".join(entries) + "]"


PUBLISHED_LISTS = {
    "only the caller": ([CALLER], True),
    "caller first": ([CALLER, OTHERS[0]], True),
    "caller between": ([OTHERS[0], CALLER, OTHERS[1]], True),
    "caller last": ([OTHERS[0], CALLER], True),
    "caller among sixty": (OTHERS[:30] + [CALLER] + OTHERS[30:], True),
    "caller taken off": (OTHERS[:3], False),
    "empty": ([], False),
    "suffix of an entry": (["rob@example.com"], False),
    "extends an entry": ([CALLER + ".au"], False),
    "ends an entry": ([OTHERS[0], "x" + CALLER], False),
}


@pytest.mark.parametrize("shared", list(PUBLISHED_LISTS))
def test_a_segment_reaches_the_caller_only_while_the_list_names_them(handler, shared) -> None:
    rules = _filter_for(handler, [CALLER])
    entries, expected = PUBLISHED_LISTS[shared]
    message = {"CallId": CALL_ID, "Channel": "REMOTE", "SharedWith": _published(entries)}
    assert _delivered(rules, message) is expected


def test_a_segment_reaches_the_caller_only_on_the_transcript_channels(handler) -> None:
    transcript = set(_load_lma_channels().STORED_TRANSCRIPT_CHANNELS)
    rules = _filter_for(handler, [CALLER])
    for channel in _channel_enum():
        message = {"CallId": CALL_ID, "Channel": channel, "SharedWith": _published([CALLER])}
        assert _delivered(rules, message) is (channel in transcript), channel


def test_a_segment_of_another_meeting_does_not_reach_the_caller(handler) -> None:
    rules = _filter_for(handler, [CALLER])
    for call_id in ("c2", "c1x", "c"):
        message = {"CallId": call_id, "Channel": "REMOTE", "SharedWith": _published([CALLER])}
        assert not _delivered(rules, message), call_id


def test_the_channels_left_out_are_the_rest_of_the_enum() -> None:
    # The filter names the channels it leaves out, so the enum and the
    # transcript's channels must account for every value: a new Channel value
    # fails here until it is placed on one side or the other.
    source = RESOLVER.read_text(encoding="utf-8")
    listed = re.search(r"const ASSISTANT_CHANNELS = \[(.*?)\];", source, re.S).group(1)
    assistant = set(re.findall(r"'([A-Z_]+)'", listed))
    transcript = set(_load_lma_channels().STORED_TRANSCRIPT_CHANNELS)
    enum = set(_channel_enum())
    assert assistant and not assistant & transcript
    assert assistant | transcript == enum


def test_a_caller_not_on_the_meetings_list_is_refused(handler) -> None:
    assert "refused" in _subscribe(handler, [OTHERS[0]])
