# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_live_context followed by a client over a whole meeting, at random.

A table stand-in evaluates the real boto3 condition objects and projection the
tool sends, and pages the way a Query does (a page is a number of rows read
before the filter, with a key after a full page). A writer adds segments as a
meeting does: a partial row first, then the final row in its place; some rows
are stored late, rewritten with new text, stamped with the same time as the
row before, stamped by a clock that is behind or ahead, or stamped with a
CreatedAt that cannot be read; some follow a pause of minutes. Text is ASCII,
Chinese, emoji or characters the encoder escapes. Writes also land between the
pages of one read.

A client polls every 5 to 12 seconds, now and then goes away for minutes and
comes back to a backlog, follows ``nextCursor`` while ``truncated`` is true,
keeps one line per ``segmentId``, sometimes loses an answer and sends the same
cursor again, and sometimes sends an older cursor again. A call lands on a
container whose clock is up to a second off, and now and then up to 20 seconds
behind. When the meeting is over it polls until nothing new arrives. It must
then hold exactly the stored transcript: no segment missing, no text out of
date, every walk ended, no cursor going backward, every answer within the caps
and its HTTP body under 100 KB.
"""

import base64
import json
import logging
import os
import random
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from boto3.dynamodb.conditions import AttributeBase, ConditionBase

os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
# The configuration layer the deployed function carries (lma_speakers, lma_consent).
LAYER = HERE.parents[2] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from tools import get_live_context  # noqa: E402

#: The largest HTTP body an answer may have.
MAX_BODY_BYTES = 100_000
T0 = datetime(2026, 11, 19, 9, 0, 0, tzinfo=timezone.utc).timestamp()


# ── a table that evaluates what boto3 is given ──────────────────────────────


def _name(operand):
    if not isinstance(operand, AttributeBase):
        raise AssertionError(f"not an attribute: {operand!r}")
    return operand.name


def evaluate(condition, item):
    """Whether ``item`` meets a boto3 condition object; unknown operators fail."""
    if not isinstance(condition, ConditionBase):
        raise AssertionError(f"not a condition: {condition!r}")
    expression = condition.get_expression()
    operator, values = expression["operator"], expression["values"]
    if operator == "AND":
        return evaluate(values[0], item) and evaluate(values[1], item)
    if operator == "OR":
        return evaluate(values[0], item) or evaluate(values[1], item)
    if operator == "NOT":
        return not evaluate(values[0], item)
    name = _name(values[0])
    present = name in item
    if operator == "=":
        return present and item[name] == values[1]
    if operator == "IN":
        return present and item[name] in list(values[1])
    if operator == "attribute_exists":
        return present
    if operator == "attribute_not_exists":
        return not present
    raise AssertionError(f"the stand-in cannot evaluate {operator}")


def project(item, projection, names):
    """The attributes a ProjectionExpression names, as DynamoDB returns them."""
    if projection is None:
        if names:
            raise AssertionError("ExpressionAttributeNames given with no expression")
        return dict(item)
    used = set()
    out = {}
    for part in projection.split(","):
        token = part.strip()
        if not token or "." in token or "[" in token:
            raise AssertionError(f"unsupported projection path {token!r}")
        if token.startswith("#"):
            if token not in (names or {}):
                raise AssertionError(f"{token} is not in ExpressionAttributeNames")
            used.add(token)
            token = names[token]
        if token in item:
            out[token] = item[token]
    unused = set(names or {}) - used
    if unused:
        raise AssertionError(f"ExpressionAttributeNames not used: {sorted(unused)}")
    return out


class FakeTranscriptTable:
    """The EventSourcing table, for one meeting row and its transcript rows.

    A Query reads ``page_rows`` rows in sort-key order, then applies the filter
    and the projection, and returns a LastEvaluatedKey after a full page.
    ``between_pages`` runs after each page that has a key, so a test can write
    while a read is under way. Every request is recorded.
    """

    name = "test-calls-table"

    def __init__(self, meeting=None, page_rows=25):
        self.rows = {}
        self.page_rows = page_rows
        self.queries = []
        self.gets = []
        self.between_pages = None
        if meeting is not None:
            self.put(meeting)

    def put(self, item):
        self.rows[(item["PK"], item["SK"])] = dict(item)

    def get_item(self, Key, ProjectionExpression=None, ExpressionAttributeNames=None, **kwargs):
        self.gets.append(dict(Key))
        item = self.rows.get((Key["PK"], Key["SK"]))
        if item is None:
            return {}
        return {"Item": project(item, ProjectionExpression, ExpressionAttributeNames)}

    def query(self, **kwargs):
        allowed = {
            "KeyConditionExpression",
            "FilterExpression",
            "ProjectionExpression",
            "ExpressionAttributeNames",
            "ExclusiveStartKey",
        }
        unknown = set(kwargs) - allowed
        if unknown:
            raise AssertionError(f"unexpected Query arguments {sorted(unknown)}")
        self.queries.append(kwargs)
        key_condition = kwargs["KeyConditionExpression"]
        partition = sorted(
            (item for (pk, _), item in self.rows.items() if evaluate(key_condition, item)),
            key=lambda item: item["SK"],
        )
        start = kwargs.get("ExclusiveStartKey")
        if start is not None:
            partition = [item for item in partition if item["SK"] > start["SK"]]
        page = partition[: self.page_rows]
        response = {
            "Items": [
                project(
                    item, kwargs.get("ProjectionExpression"), kwargs.get("ExpressionAttributeNames")
                )
                for item in page
                if "FilterExpression" not in kwargs or evaluate(kwargs["FilterExpression"], item)
            ]
        }
        if len(page) == self.page_rows:
            response["LastEvaluatedKey"] = {"PK": page[-1]["PK"], "SK": page[-1]["SK"]}
            if self.between_pages is not None:
                self.between_pages()
        return response


def meeting_row(call_id="live-1", owner="me@example.com", status="STARTED", **extra):
    row = {
        "PK": f"c#{call_id}",
        "SK": f"c#{call_id}",
        "Owner": owner,
        "Status": status,
        "CreatedAt": "2026-11-19T09:00:00.000Z",
    }
    row.update(extra)
    return row


def iso(seconds):
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def decode(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))


# ── the meeting, as a list of writes ────────────────────────────────────────

BAD_STAMPS = (
    None,  # no CreatedAt
    "",
    "not a time",
    "2026-02-30T10:00:00Z",
    Decimal("1794999999"),
    "far future",  # replaced by a time a day ahead of the writer's clock
    "beyond five minutes",  # replaced by a time 400 seconds ahead
)


#: What a line says, repeated: ASCII, Chinese, emoji and characters JSON escapes.
TEXT_KINDS = ("word ", "word ", "会议记录", "🙂👍 ", 'say "\\x" ')


class Meeting:
    """The writes of one meeting: (visible at, item), in meeting seconds."""

    def __init__(self, rng, call_id, lines, bad_rows, pauses=False):
        self.rng = rng
        self.call_id = call_id
        self.writes = []
        self.final = {}
        clock = 0.0
        bad = set(rng.sample(range(lines), bad_rows)) if bad_rows else set()
        previous_stamp = None  # (seconds, text) of the last readable stamp
        for index in range(lines):
            clock += rng.uniform(1.0, 7.0)  # a line about every 4 seconds
            if pauses and rng.random() < 0.06:
                clock += rng.uniform(70.0, 300.0)  # nobody spoke for minutes
            segment = f"{rng.getrandbits(64):016x}"  # ids do not sort by time
            channel = rng.choices(
                ["REMOTE", "LOCAL", "CALLER", "AGENT_ASSISTANT", "MEETING_CHAT"],
                weights=[50, 30, 5, 8, 7],
            )[0]
            text = f"line {index} " + rng.choice(TEXT_KINDS) * rng.randint(1, 30)
            if channel == "MEETING_CHAT":
                text = f"chat {index}\nsecond line\n\nthird"
            start, end = clock, clock + rng.uniform(0.5, 4.0)
            base = {
                "PK": f"trs#{call_id}",
                "SK": f"s#{segment}",
                "SegmentId": segment,
                "Channel": channel,
                "Speaker": rng.choice(["spk_0", "spk_1", "Ana", ""]),
                "StartTime": Decimal(str(round(start, 3))),
                "EndTime": Decimal(str(round(end, 3))),
            }
            # The partial row first, replaced in place by the final one.
            self.writes.append((start + 0.3, {**base, "Transcript": "partial", "IsPartial": True}))
            if index in bad:
                delay = rng.uniform(0.5, 3.0)
                stamp = rng.choice(BAD_STAMPS)
                if stamp == "far future":
                    stamp = iso(T0 + end + delay + 86400)
                elif stamp == "beyond five minutes":
                    stamp = iso(T0 + end + delay + 400)
            else:
                late = rng.random() < 0.1
                delay = rng.uniform(10.0, 90.0) if late else rng.uniform(0.5, 3.0)
                skew = rng.uniform(-15.0, 15.0)
                if rng.random() < 0.03:
                    skew = rng.uniform(60.0, 240.0)  # a writer clock well ahead
                written = end + delay + skew
                if (
                    previous_stamp is not None
                    and previous_stamp[0] >= end + 0.5
                    and rng.random() < 0.05
                ):
                    # A tie: stored in the same millisecond as the row before.
                    written = previous_stamp[0]
                    delay, skew = written - end, 0.0
                stamp = iso(T0 + written)
                previous_stamp = (written, stamp)
            visible = end + delay + rng.uniform(0.0, 25.0)
            item = {**base, "Transcript": text, "IsPartial": False}
            if stamp is not None:
                item["CreatedAt"] = stamp
            self.writes.append((visible, item))
            last = (visible, item)
            if index not in bad and rng.random() < 0.1:
                # Rewritten in place later: new text, new CreatedAt, same key.
                again = visible + rng.uniform(1.0, 120.0)
                rewritten = {
                    **item,
                    "Transcript": text + " (revised)",
                    "CreatedAt": iso(T0 + again - rng.uniform(0.0, 20.0) + rng.uniform(-5, 5)),
                }
                self.writes.append((again, rewritten))
                last = (again, rewritten)
            if last[0] >= self.final.get(segment, (-1, None))[0]:
                self.final[segment] = last
        self.writes.sort(key=lambda write: write[0])
        self.end = max(visible for visible, _ in self.writes)

    def expected(self, include_chat=True):
        return {
            segment: get_live_context.cut_text(get_live_context.line_text(item))
            for segment, (_, item) in self.final.items()
            if include_chat or item["Channel"] != "MEETING_CHAT"
        }


class Simulation:
    """One meeting, one table, one client."""

    def __init__(self, seed, max_lines, bad_rows, lines=450, pauses=False):
        self.rng = random.Random(seed)
        self.meeting = Meeting(self.rng, f"sim-{seed}", lines, bad_rows, pauses)
        self.table = FakeTranscriptTable(meeting_row(self.meeting.call_id))
        self.table.between_pages = self._during_read
        self.max_lines = max_lines
        self.now = -20.0
        self.applied = 0
        self.calls = 0
        self.polls = 0
        self.max_calls_in_walk = 0
        self.repeats = 0
        self.received = 0
        self.problems = []

    def _apply_until(self, moment):
        writes = self.meeting.writes
        while self.applied < len(writes) and writes[self.applied][0] <= moment:
            self.table.put(writes[self.applied][1])
            self.applied += 1

    def _during_read(self):
        self.now += 0.02
        self._apply_until(self.now)

    def _lambda_clock(self):
        # Each call may land on a container whose clock is a little off, and
        # now and then on one that is well behind.
        behind = self.rng.uniform(0.0, 20.0) if self.rng.random() < 0.05 else 0.0
        return T0 + self.now + self.rng.uniform(-1.0, 1.0) - behind

    def _call(self, cursor):
        self.calls += 1
        self._apply_until(self.now)
        moment = self._lambda_clock()
        return get_live_context.execute(
            meeting_id=self.meeting.call_id,
            cursor=cursor,
            max_lines=self.max_lines,
            include_chat=True,
            user_id="me@example.com",
            now=lambda: moment,
            timer=lambda: 0.0,
            table=self.table,
        )

    def _check_answer(self, answer):
        lines = answer["lines"]
        if len(lines) > self.max_lines:
            self.problems.append(f"{len(lines)} lines over maxLines")
        units = sum(
            get_live_context.LINE_UNITS
            + sum(get_live_context.units_of(value) for value in line.values())
            for line in lines
        )
        if len(lines) > 1 and units > get_live_context.MAX_PAGE_UNITS:
            self.problems.append("an answer over the size budget")
        if len(body(answer)) >= MAX_BODY_BYTES:
            self.problems.append("an answer body of 100 KB or more")
        if any(len(line["text"]) > get_live_context.MAX_LINE_CHARS + 6 for line in lines):
            self.problems.append("a line over the line cap")

    def walk(self, cursor, last_watermark, held):
        """One poll: calls until the answer is not truncated. Returns the new cursor."""
        calls = 0
        after = None
        new = 0
        while True:
            calls += 1
            if calls > 2000:
                self.problems.append("a walk that did not end")
                return cursor, last_watermark, new
            answer = self._call(cursor)
            if self.rng.random() < 0.05:
                # The answer is lost on the way; the client asks again.
                self.now += 0.5
                continue
            self._check_answer(answer)
            for line in answer["lines"]:
                self.received += 1
                if held.get(line["segmentId"]) == line["text"]:
                    self.repeats += 1
                else:
                    held[line["segmentId"]] = line["text"]
                    new += 1
            state = decode(answer["nextCursor"])
            # Inside a walk the cursor keeps the watermark it started from (an
            # older one, when the client sent an older cursor) and carries the
            # walk's clock; the watermark a walk ends with is that clock.
            reached = state["n"] if answer["truncated"] else state["w"]
            if reached < last_watermark:
                self.problems.append("the watermark went backward")
            if answer["truncated"]:
                key = tuple(state["a"])
                if after is not None and key <= after:
                    self.problems.append("a continuation did not move forward")
                after = key
                cursor = answer["nextCursor"]
                self.now += self.rng.uniform(0.05, 0.4)
                continue
            self.max_calls_in_walk = max(self.max_calls_in_walk, calls)
            return answer["nextCursor"], state["w"], new

    def run(self):
        held = self.held = {}
        cursor, watermark = None, 0.0
        previous = None
        quiet_polls = 0
        while True:
            self.polls += 1
            sent = cursor
            if previous is not None and self.rng.random() < 0.03:
                sent = previous  # an older cursor sent again
            # No answer goes behind the cursor it was given.
            floor = decode(sent)["w"] if sent else 0.0
            next_cursor, watermark, new = self.walk(sent, floor, held)
            previous, cursor = cursor, next_cursor
            if self.now > self.meeting.end + 5:
                quiet_polls = quiet_polls + 1 if new == 0 else 0
                if quiet_polls >= 2:
                    break
            if self.now > self.meeting.end + 600:
                self.problems.append("new lines kept arriving after the meeting")
                break
            self.now += self.rng.uniform(5.0, 12.0)
            if self.rng.random() < 0.03:
                # The client is away for a while and comes back to a backlog.
                self.now += self.rng.uniform(120.0, 400.0)
        expected = self.meeting.expected()
        missing = sorted(set(expected) - set(held))
        stale = sorted(s for s in expected if s in held and held[s] != expected[s])
        unknown = sorted(set(held) - set(expected))
        return {
            "missing": len(missing),
            "stale": len(stale),
            "unknown": len(unknown),
            "problems": self.problems,
            "calls": self.calls,
            "polls": self.polls,
            "max_calls_in_walk": self.max_calls_in_walk,
            "repeats_per_poll": self.repeats / max(1, self.polls),
            "segments": len(expected),
        }


def body(answer):
    """The HTTP body index.lambda_handler sends for an answer."""
    text = json.dumps(answer, indent=2, default=str)
    message = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
    return json.dumps(message)


def run_cell(seed, max_lines, bad_rows, lines=450, pauses=False):
    with patch.object(get_live_context, "live_context_state", return_value=get_live_context.ON):
        return Simulation(seed, max_lines, bad_rows, lines, pauses).run()


class TestFollowingAMeeting(unittest.TestCase):
    """3 seeds for each of maxLines 20 and 200, with 0, 30 and 300 unreadable stamps.

    And with pauses of minutes between lines, where a row with no readable
    stamp cannot borrow the time of the row before the pause.
    """

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def _cell(self, max_lines, bad_rows, pauses=False):
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                result = run_cell(
                    seed * 1000 + max_lines + bad_rows, max_lines, bad_rows, pauses=pauses
                )
                self.assertEqual(result["problems"], [])
                self.assertEqual(result["missing"], 0, result)
                self.assertEqual(result["stale"], 0, result)
                self.assertEqual(result["unknown"], 0, result)

    def test_max_lines_20_clean(self):
        self._cell(20, 0)

    def test_max_lines_20_with_30_unreadable_stamps(self):
        self._cell(20, 30)

    def test_max_lines_20_with_300_unreadable_stamps(self):
        self._cell(20, 300)

    def test_max_lines_200_clean(self):
        self._cell(200, 0)

    def test_max_lines_200_with_30_unreadable_stamps(self):
        self._cell(200, 30)

    def test_max_lines_200_with_300_unreadable_stamps(self):
        self._cell(200, 300)

    def test_max_lines_20_with_pauses_and_60_unreadable_stamps(self):
        self._cell(20, 60, pauses=True)

    def test_max_lines_200_with_pauses_and_300_unreadable_stamps(self):
        self._cell(200, 300, pauses=True)

    def test_max_lines_1000_with_pauses(self):
        self._cell(1000, 30, pauses=True)


if __name__ == "__main__":
    unittest.main()
