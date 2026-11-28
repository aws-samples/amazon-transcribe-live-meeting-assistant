# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Fact checks and lookups through the analyzer's plumbing, on the recorded meeting.

Off by default, a run asks for no candidates and invokes nothing. On, the run's
candidates wait on its state row; a research run takes a batch under its own
lease, invokes the meeting assistant function, and publishes what holds up,
within the cap the rest of the guidance shares. Each test asserts one of: the
request names the owner, the sources and the allowlist; a candidate is checked
once; a dismissed item's claim is not published again; the per-meeting caps
hold; nothing is published once the meeting ended; a deployment with no source
stops asking; a failed request is not retried.
"""

from __future__ import annotations

import io
import json
import re

import pytest
from test_index import (
    CALL_ID,
    OWNER,
    RECORDED,
    FakeDynamo,
    _conditional_failure,
    _value,
    answer,
    automatic,
    index,
    later,
    name_of,
)
from test_index import world as base_world  # noqa: F401 -- the fixture, used through world

ON = {"factChecksEnabled": True, "lookupsEnabled": True}

PRICE = {
    "kind": "fact_check",
    "subject": "renewal price",
    "claim": "The renewal is $42,000 a year on a two-year term.",
    "segment": name_of("seg-04"),
    "quote": "forty two thousand a year on a two year term",
    "why": "The price decision rests on it.",
    "confidence": 0.9,
}
HOURS = {
    "kind": "lookup",
    "subject": "extended support hours",
    "claim": "extended support hours",
    "segment": name_of("seg-06"),
    "quote": "extended support hours",
    "confidence": 0.85,
}
# A candidate from the second run, which is shown only the segments said since
# the first (later() adds them, S1 being the first).
LATE = {
    "kind": "lookup",
    "subject": "renewal point zero",
    "claim": "renewal point zero",
    "segment": "S1",
    "quote": "More on the renewal, point 0",
    "confidence": 0.85,
}
SOURCES = {
    "R1": {"title": "Support price list", "url": "https://example.com/prices", "via": "Documents"}
}


def with_checks(*entries, **fields):
    value = answer(**fields)
    value["checks"] = list(entries)
    return value


def contradicted(candidate_id="C1", **fields):
    value = {
        "id": candidate_id,
        "kind": "fact_check",
        "verdict": "contradicted",
        "confidence": 0.9,
        "summary": "The price list gives $46,000 a year for this scope.",
        "sources": ["R1"],
    }
    value.update(fields)
    return value


class TwoRowDynamo(FakeDynamo):
    """FakeDynamo with the run's row and the research row kept apart by sort key."""

    def __init__(self, base):
        self.__dict__.update(base.__dict__)
        self.rows = {"analyzer": self.state, "research": {}}

    def _select(self, key):
        if key["PK"]["S"].startswith("gd#"):
            self.state = self.rows[key["SK"]["S"]]

    def get_item(self, TableName, Key):  # noqa: N803 - boto3's names
        self._select(Key)
        return super().get_item(TableName=TableName, Key=Key)

    def update_item(self, **kwargs):
        self._select(kwargs["Key"])
        expression = kwargs["UpdateExpression"]
        values = {k: _value(v) for k, v in kwargs["ExpressionAttributeValues"].items()}
        condition = kwargs.get("ConditionExpression")
        if condition is None:
            # A run's append to the queue and the drop log: no lease.
            if "Pending = list_append" in expression:
                self.state["Pending"] = list(self.state.get("Pending") or []) + values[":new"]
            if "DropLog = list_append" in expression:
                self.state["DropLog"] = list(self.state.get("DropLog") or []) + values[":drops"]
            self.state["CallId"] = values[":call"]
            return {}
        if condition == "attribute_exists(PK)" and "DropLog = list_append" in expression:
            if not self.state:
                raise _conditional_failure()
            self.state["DropLog"] = list(self.state.get("DropLog") or []) + values[":d"]
            return {}
        if "Pending = :empty" in expression:
            assert condition.startswith("attribute_exists(PK) AND")
            lease = self.state.get("LeaseUntil")
            if not self.state or (lease is not None and lease >= values[":now"]):
                raise _conditional_failure()
            self.state["Pending"] = []
            self.state["DropLog"] = list(self.state.get("DropLog") or []) + values[":d"]
            return {}
        taken = len(re.findall(r"Pending\[\d+\]", expression))
        cut = len(re.findall(r"DropLog\[\d+\]", expression))
        result = super().update_item(**kwargs)
        if taken:
            self.state["Pending"] = list(self.state.get("Pending") or [])[taken:]
        if cut:
            self.state["DropLog"] = list(self.state.get("DropLog") or [])[cut:]
        return result


@pytest.fixture
def world(base_world, monkeypatch):  # noqa: F811  # pylint: disable=redefined-outer-name
    def setup(research=None, **kwargs):
        settings = {**ON, **(kwargs.pop("settings", None) or {})}
        state = base_world(settings=settings, **kwargs)
        dynamo = TwoRowDynamo(state["dynamo"])
        monkeypatch.setattr(index, "dynamodb", dynamo)
        state["dynamo"] = dynamo
        requests = []
        replies = list(research or [{"ok": True, "findings": [contradicted()], "sources": SOURCES}])

        def invoke(request, seconds):
            requests.append((request, seconds))
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(index, "MEETING_ASSIST_FUNCTION_ARN", "STRANDS-LMA-MeetingAssist")
        monkeypatch.setattr(index, "invoke_research", invoke)
        state["requests"] = requests
        return state

    return setup


def research_row(state):
    return state["dynamo"].rows["research"]


def checked(state):
    return json.loads(research_row(state).get("Checked") or "{}")


def facts(state):
    return [p for p in state["published"] if p["type"] in ("fact_check", "lookup")]


# ── off by default ───────────────────────────────────────────────────────────


def test_off_by_default_a_run_asks_for_no_candidates_and_researches_nothing(
    base_world,  # noqa: F811
    monkeypatch,
):
    state = base_world(answers=[with_checks(PRICE)])
    monkeypatch.setattr(index, "MEETING_ASSIST_FUNCTION_ARN", "STRANDS-LMA-MeetingAssist")
    monkeypatch.setattr(index, "invoke_research", lambda *a: pytest.fail("researched"))
    assert automatic()["ran"] is True
    (call,) = state["bedrock"].calls
    schema = call["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert "checks" not in schema["properties"]
    assert "Checks to research" not in call["system"][0]["text"]
    assert "<checks_done>" not in call["messages"][0]["content"][-1]["text"]
    assert "PendingChecks" not in state["dynamo"].state


def test_on_a_run_asks_for_the_kinds_that_are_on(world):
    state = world(settings={"lookupsEnabled": False}, answers=[answer()])
    automatic()
    (call,) = state["bedrock"].calls
    schema = call["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert schema["properties"]["checks"]["items"]["properties"]["kind"]["enum"] == ["fact_check"]
    assert "Report only the kinds that are on: fact_check." in call["system"][0]["text"]
    assert "<checks_done>\n(none yet)\n</checks_done>" in call["messages"][0]["content"][-1]["text"]


# ── a research run ───────────────────────────────────────────────────────────


def test_a_claim_is_researched_and_published_with_its_source(world):
    state = world(answers=[with_checks(PRICE)])
    result = automatic()
    assert result["research"] == {
        "runs": 1,
        "researched": 1,
        "findings": 1,
        "published": 1,
        "deferred": 0,
    }
    ((request, seconds),) = state["requests"]
    assert request["action"] == "guidance_research" and request["callId"] == CALL_ID
    assert request["ownerEmail"] == OWNER
    assert request["sources"] == {
        "documents": True,
        "pastMeetings": True,
        "webSearch": True,
        "mcpServers": False,
    }
    assert request["maxToolCalls"] == 6 and request["deadlineSeconds"] == 60 == seconds
    (candidate,) = request["candidates"]
    assert candidate["id"] == "C1" and candidate["quote"] == PRICE["quote"]
    assert "Lee: The renewal is forty two thousand" in candidate["context"]
    (item,) = facts(state)
    assert item["type"] == "fact_check" and item["source"] == "live-guidance"
    assert item["title"] == "Possibly inaccurate: renewal price"
    assert item["dedup_key"] == "fact_check:claim:renewal-price"
    assert item["refs"] == ["segment:seg-04"]
    assert "(<https://example.com/prices>)" in item["body"]
    record = checked(state)["fact_check:claim:renewal-price"]
    assert record["verdict"] == "contradicted" and record["notificationId"]
    assert "LeaseUntil" not in research_row(state) and research_row(state)["RunCount"] == 1
    assert research_row(state)["Pending"] == []


def test_the_allowlisted_mcp_tools_are_named_in_the_request(world):
    state = world(
        answers=[with_checks(PRICE)],
        settings={"researchMcpTools": ["Tickets/search"], "researchWebSearch": False},
    )
    automatic()
    ((request, _),) = state["requests"]
    assert request["mcpTools"] == ["Tickets/search"]
    assert request["sources"]["mcpServers"] is True and request["sources"]["webSearch"] is False


def test_a_finding_that_is_not_worth_publishing_is_recorded_only(world):
    state = world(
        answers=[with_checks(PRICE)],
        research=[
            {"ok": True, "findings": [contradicted(verdict="supported")], "sources": SOURCES}
        ],
    )
    automatic()
    assert facts(state) == []
    assert checked(state)["fact_check:claim:renewal-price"]["verdict"] == "supported"


def test_a_lookup_is_published(world):
    state = world(
        answers=[with_checks(HOURS)],
        research=[
            {
                "ok": True,
                "findings": [
                    contradicted(
                        kind="lookup", verdict="answered", summary="Weekdays 7:00 to 22:00 Eastern."
                    )
                ],
                "sources": SOURCES,
            }
        ],
    )
    automatic()
    (item,) = facts(state)
    assert item["type"] == "lookup" and item["title"] == "Lookup: extended support hours"


def test_a_claim_is_checked_once(world):
    state = world(
        answers=[with_checks(PRICE), with_checks({**PRICE, "subject": "price of the renewal"})]
    )
    automatic()
    later(state)
    automatic()
    assert len(state["requests"]) == 1
    (call_1, call_2) = state["bedrock"].calls
    assert "subject=renewal price" in call_2["messages"][0]["content"][-1]["text"]


def test_a_dismissed_item_is_named_to_the_run_and_not_published_again(world):
    state = world(answers=[with_checks(PRICE), answer()])
    automatic()
    notification_id = checked(state)["fact_check:claim:renewal-price"]["notificationId"]
    state["dynamo"].notes.append(
        {"PK": f"nt#{CALL_ID}", "SK": "u#dana", "Dismissed": [notification_id]}
    )
    later(state)
    automatic()
    text = state["bedrock"].calls[-1]["messages"][0]["content"][-1]["text"]
    assert "(dismissed: report nothing like it)" in text


# ── caps ─────────────────────────────────────────────────────────────────────


CLAIMS = [
    "The renewal costs $42,000 a year.",
    "Support covers seven in the morning until ten at night.",
    "The migration team is booked until February.",
    "Annual billing is invoiced in January.",
    "The quote is sent by Friday.",
]


def five_claims():
    return [{**PRICE, "subject": f"claim {n}", "claim": claim} for n, claim in enumerate(CLAIMS)]


def waiting(state):
    return [json.loads(value) for value in research_row(state).get("Pending") or []]


def drop_log(state):
    return [json.loads(value) for value in research_row(state).get("DropLog") or []]


def test_every_candidate_from_a_run_is_researched_in_batches_of_three_in_one_invocation(world):
    state = world(answers=[with_checks(*five_claims())])
    result = automatic()
    assert [[c["claim"] for c in r["candidates"]] for r, _ in state["requests"]] == [
        CLAIMS[:3],
        CLAIMS[3:],
    ]
    assert result["research"]["runs"] == 2 and result["research"]["deferred"] == 0
    assert waiting(state) == [] and len(checked(state)) == 5
    assert research_row(state)["RunCount"] == 2 and drop_log(state) == []


def test_candidates_wait_while_another_research_run_holds_the_meeting(world):
    state = world(answers=[with_checks(*five_claims()), with_checks(LATE)])
    research_row(state)["LeaseUntil"] = state["clock"].now + 60
    automatic()
    assert state["requests"] == []
    assert [c["claim"] for c in waiting(state)] == CLAIMS
    research_row(state).pop("LeaseUntil")
    later(state)
    automatic()
    # The queue drains oldest first: the five waiting, then the new one.
    assert [[c["claim"] for c in r["candidates"]] for r, _ in state["requests"]] == [
        CLAIMS[:3],
        CLAIMS[3:] + ["renewal point zero"],
    ]
    assert waiting(state) == []


def test_what_does_not_fit_before_the_deadline_waits_and_the_end_run_researches_it(world):
    every = [contradicted(f"C{n}") for n in range(1, 4)]
    state = world(
        answers=[with_checks(*five_claims())],
        research=[{"ok": True, "findings": every, "sources": SOURCES}],
    )
    # An invocation with too little time left for a research run: everything waits.
    index.run(CALL_ID, until=index.time.monotonic() + 30)
    assert state["requests"] == [] and len(waiting(state)) == 5
    # The meeting ends; its run at the end researches the queue and may publish.
    state["dynamo"].meeting["Status"] = "ENDED"
    result = index.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, None)
    assert result["ran"] is True and result["research"]["runs"] == 2
    assert waiting(state) == [] and len(facts(state)) == 5


def test_the_end_run_reads_the_last_minutes_no_run_read_and_researches_their_claims(world):
    lookup_found = [contradicted(kind="lookup", verdict="answered", summary="An audit report.")]
    state = world(
        answers=[answer(), with_checks(LATE)],
        research=[{"ok": True, "findings": lookup_found, "sources": SOURCES}],
    )
    automatic()
    assert state["requests"] == []
    # The meeting's last minutes, after which nobody speaks: no run reads them.
    later(state)
    state["dynamo"].meeting["Status"] = "ENDED"
    result = index.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, None)
    assert result["found"] == 1 and result["research"]["runs"] == 1
    end_call = state["bedrock"].calls[-1]
    assert "S1 [" in end_call["messages"][0]["content"][-1]["text"]
    assert (
        "the meeting has ended: report checks only"
        in end_call["messages"][0]["content"][-1]["text"]
    )
    (item,) = facts(state)
    assert item["type"] == "lookup"
    # No other guidance is published at the end, and the run is counted.
    assert [p["type"] for p in state["published"]] == ["lookup"]
    assert state["dynamo"].rows["analyzer"]["RunCount"] == 2


def test_the_end_run_right_after_a_batch_still_researches_what_waits(world, monkeypatch):
    every = [contradicted(f"C{n}") for n in range(1, 4)]
    state = world(
        answers=[with_checks(*five_claims())],
        research=[{"ok": True, "findings": every, "sources": SOURCES}],
    )
    # Time for one batch only (each reading of the clock is a minute later),
    # then END a few seconds later.
    ticks = {"now": 0.0}

    def monotonic():
        ticks["now"] += 60.0
        return ticks["now"]

    real_monotonic = index.time.monotonic
    monkeypatch.setattr(index.time, "monotonic", monotonic)
    # The first check reads 60 (60 + 60 + 60 < 181), the second 120: one batch.
    index.run(CALL_ID, until=181.0)
    monkeypatch.setattr(index.time, "monotonic", real_monotonic)
    assert len(state["requests"]) == 1 and len(waiting(state)) == 2
    state["clock"].now += 5
    state["dynamo"].meeting["Status"] = "ENDED"
    result = index.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, None)
    assert result["research"]["runs"] == 1 and waiting(state) == []
    assert len(facts(state)) == 5


def test_candidates_nothing_was_searched_for_are_logged_not_counted(world):
    state = world(
        answers=[with_checks(PRICE)],
        research=[{"ok": False, "reason": "no sources", "findings": []}],
    )
    automatic()
    assert checked(state) == {}
    assert [(d["subject"], d["reason"]) for d in drop_log(state)] == [
        ("renewal price", "no sources")
    ]


def test_only_the_strands_meeting_assistant_is_sent_research():
    pattern = index._STRANDS_ASSISTANT  # pylint: disable=protected-access
    assert pattern.search(
        "arn:aws:lambda:us-east-1:123456789012:function:STRANDS-LMA-Live-MeetingAssist"
    )
    assert not pattern.search("arn:aws:lambda:us-east-1:123456789012:function:my-agent-assist")
    assert not pattern.search("")


def test_the_end_run_does_nothing_without_a_queue_or_with_the_types_off(world):
    world(answers=[answer()])
    assert index.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, None)["reason"] == (
        "nothing waits for research"
    )
    world(answers=[answer()], settings={"factChecksEnabled": False, "lookupsEnabled": False})
    assert index.handler({"CallId": CALL_ID, "Trigger": "meeting-ended"}, None)["ran"] is False


def test_a_full_queue_drops_the_rest_and_logs_why(world):
    state = world(answers=[with_checks(*five_claims())])
    research_row(state)["LeaseUntil"] = state["clock"].now + 60
    research_row(state)["Pending"] = [
        json.dumps(
            {
                **index.checks.Check(
                    "lookup",
                    f"t{n}",
                    f"term number {n}",
                    "q",
                    "seg-01",
                    "A",
                    0,
                    "",
                    0.9,
                    f"lookup:term:t{n}",
                ).to_record()
            }
        )
        for n in range(8)
    ]
    automatic()
    assert len(waiting(state)) == index.checks.MAX_PENDING
    assert [(d["subject"], d["reason"]) for d in drop_log(state)] == [
        ("claim 2", "queue full"),
        ("claim 3", "queue full"),
        ("claim 4", "queue full"),
    ]


def test_candidates_a_run_rejects_are_logged_with_their_reasons(world):
    bad = {**PRICE, "subject": "misquoted", "quote": "forty five thousand a year"}
    state = world(
        answers=[with_checks(PRICE, bad, {**PRICE, "subject": "unsure", "confidence": 0.3})]
    )
    automatic()
    assert [(d["subject"], d["reason"]) for d in drop_log(state)] == [
        ("misquoted", "quote not in its segment"),
        ("unsure", "below the confidence threshold"),
    ]


def test_the_candidates_per_meeting_cap_holds_and_logs_the_drop(world):
    state = world(
        answers=[with_checks(PRICE, HOURS), with_checks(LATE)],
        settings={"researchChecksPerMeeting": 1},
    )
    automatic()
    assert len(state["requests"]) == 1
    assert [d["reason"] for d in drop_log(state)] == ["claims and terms per meeting reached"]
    later(state)
    automatic()
    assert len(state["requests"]) == 1
    # With the cap reached, the run is not asked for more.
    schema = state["bedrock"].calls[-1]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert "checks" not in schema["properties"]


def test_the_research_runs_cap_holds_and_what_waits_is_dropped_and_logged(world):
    state = world(answers=[with_checks(*five_claims())], settings={"researchRunsPerMeeting": 1})
    result = automatic()
    assert len(state["requests"]) == 1 and result["research"]["runs"] == 1
    assert waiting(state) == []
    assert [d["reason"] for d in drop_log(state)] == ["research runs per meeting reached"] * 2


def test_once_the_research_runs_are_used_the_run_asks_for_no_more_candidates(world):
    state = world(
        answers=[with_checks(PRICE), with_checks(LATE)], settings={"researchRunsPerMeeting": 1}
    )
    automatic()
    later(state)
    automatic()
    schema = state["bedrock"].calls[-1]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert "checks" not in schema["properties"]
    assert len(state["requests"]) == 1


def test_nothing_is_queued_once_the_transcript_is_deleted(world, monkeypatch):
    state = world(answers=[with_checks(PRICE)])
    real = index.read_meeting
    reads = []

    def read_meeting(call_id):
        reads.append(call_id)
        meeting = real(call_id)
        # Deleted while the run was making its model call.
        return (
            {**meeting, "TranscriptDeletedAt": "2026-11-28T00:00:00Z"}
            if len(reads) > 1
            else meeting
        )

    monkeypatch.setattr(index, "read_meeting", read_meeting)
    automatic()
    assert research_row(state) == {} and state["requests"] == []


def test_an_agent_assist_function_that_does_not_research_pauses_the_candidates(world):
    state = world(
        answers=[with_checks(PRICE), with_checks(LATE)],
        research=[{"message": "Here is my answer to your question."}],
    )
    automatic()
    assert research_row(state)["UnavailableAt"]
    later(state)
    automatic()
    schema = state["bedrock"].calls[-1]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert "checks" not in schema["properties"] and len(state["requests"]) == 1


def test_fact_checks_share_the_cap_on_guidance_per_ten_minutes(world):
    guidance = [
        {
            "type": "clarify",
            "subject": "support hours time zone",
            "segments": [name_of("seg-08")],
            "title": "Ask which time zone the hours are in",
            "body": "The answer was 'flexible'.",
            "confidence": 0.9,
        }
    ]
    state = world(answers=[with_checks(PRICE, guidance=guidance)], settings={"maxItemsPer10Min": 1})
    automatic()
    assert [p["type"] for p in state["published"]] == ["clarify"]
    assert checked(state)["fact_check:claim:renewal-price"]["notificationId"] is None


# ── stopping ─────────────────────────────────────────────────────────────────


def test_a_batch_under_way_when_the_meeting_ends_still_publishes(world, monkeypatch):
    state = world(answers=[with_checks(PRICE)])
    reads = []
    real = index.read_meeting

    def read_meeting(call_id):
        reads.append(call_id)
        meeting = real(call_id)
        return {**meeting, "Status": "ENDED"} if len(reads) > 3 else meeting

    monkeypatch.setattr(index, "read_meeting", read_meeting)
    automatic()
    assert len(state["requests"]) == 1 and len(facts(state)) == 1


def test_nothing_is_published_once_the_transcript_is_deleted(world, monkeypatch):
    state = world(answers=[with_checks(PRICE)])
    reads = []
    real = index.read_meeting

    def read_meeting(call_id):
        reads.append(call_id)
        meeting = real(call_id)
        deleted = {"TranscriptDeletedAt": "2026-11-28T00:00:00Z"}
        return {**meeting, **deleted} if len(reads) > 3 else meeting

    monkeypatch.setattr(index, "read_meeting", read_meeting)
    automatic()
    assert len(state["requests"]) == 1 and facts(state) == []
    assert "fact_check:claim:renewal-price" in checked(state)


def test_a_deployment_with_no_source_stops_asking_for_candidates(world):
    state = world(
        answers=[with_checks(PRICE), with_checks(HOURS)],
        research=[{"ok": False, "reason": "no sources", "findings": []}],
    )
    automatic()
    assert research_row(state)["UnavailableAt"]
    later(state)
    automatic()
    assert len(state["requests"]) == 1
    schema = state["bedrock"].calls[-1]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert "checks" not in schema["properties"]


def test_a_failed_request_is_recorded_and_not_retried(world):
    state = world(
        answers=[with_checks(PRICE), with_checks(PRICE)], research=[TimeoutError("read timeout")]
    )
    result = automatic()
    assert result["ran"] is True and result["research"]["published"] == 0
    assert checked(state)["fact_check:claim:renewal-price"]["verdict"] == "failed"
    later(state)
    automatic()
    assert len(state["requests"]) == 1


def test_without_the_meeting_assistant_no_candidate_is_asked_for(world, monkeypatch):
    state = world(answers=[with_checks(PRICE)])
    monkeypatch.setattr(index, "MEETING_ASSIST_FUNCTION_ARN", "")
    automatic()
    assert state["requests"] == []
    schema = state["bedrock"].calls[0]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert "checks" not in schema["properties"]


# ── the request itself ───────────────────────────────────────────────────────


class FakeLambda:
    def __init__(self, reply, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        response = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.reply).encode())}
        if self.error:
            response["FunctionError"] = self.error
        return response


def test_the_research_request_waits_past_its_deadline_and_reads_the_answer(monkeypatch):
    fake = FakeLambda({"ok": True, "findings": []})
    timeouts = []
    monkeypatch.setattr(index, "_lambda_client", lambda seconds: timeouts.append(seconds) or fake)
    monkeypatch.setattr(index, "MEETING_ASSIST_FUNCTION_ARN", "STRANDS-LMA-MeetingAssist")
    assert index.invoke_research({"callId": CALL_ID}, 60) == {"ok": True, "findings": []}
    (call,) = fake.calls
    assert call["InvocationType"] == "RequestResponse"
    assert call["FunctionName"] == "STRANDS-LMA-MeetingAssist"
    assert timeouts == [105]


def test_a_function_error_is_a_failed_request(monkeypatch):
    monkeypatch.setattr(index, "_lambda_client", lambda seconds: FakeLambda({}, error="Unhandled"))
    with pytest.raises(index.ResearchError):
        index.invoke_research({}, 60)


def test_only_web_links_survive_from_the_answer():
    sources = index._sources(  # pylint: disable=protected-access
        {
            "R1": {"title": "A", "url": "https://example.com/a"},
            "R2": {"title": "B", "url": "javascript:alert(1)"},
            "R3": {"title": "C", "url": "https://example.com/x y"},
            "R4": "not a source",
        }
    )
    assert [s["url"] for s in sources.values()] == ["https://example.com/a", "", ""]


def test_recorded_meeting_owner_is_the_one_researched_for():
    assert RECORDED["meeting"]["Owner"] == OWNER
