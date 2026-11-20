# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting lifecycle events on a live stack, caught on the stack's own event bus.

Creates a temporary SQS queue and a temporary rule on the meeting event bus
(MeetingEventBusName) matching one synthetic meeting, then drives that meeting
through every transition that publishes an event:

* START, ADD_SUMMARY and END on the call data stream, as the capture clients and
  the summary orchestrator write them -> Meeting Started, Summary Ready and
  Meeting Ended from the call event processor;
* a synthetic Virtual Participant row in the Virtual Participant table, written
  and moved through JOINING to MANUAL_ACTION_REQUIRED, with one write in between
  that leaves the status alone -> Virtual Participant Status Changed, read from
  the table's stream. The row is not scheduled, so the scheduler ignores it, and
  nothing launches.

Then reads the queue until all four detail-types have arrived or the time is up,
and returns what it saw. Everything it made -- the rule and its target, the
queue, the meeting's rows and the Virtual Participant row -- is deleted by
``cleanup``.

Used by test_meeting_events.py, and runnable on its own:

    AWS_PROFILE=default python integ-tests/meeting_events_probe.py <stack-name>
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3

DETAIL_TYPES = (
    "Meeting Started",
    "Meeting Ended",
    "Summary Ready",
    "Virtual Participant Status Changed",
)
SUMMARY_TEXT = "Integration summary text that no event may carry."
LABEL = {"Key": "integ-events", "Value": "probe"}


def _outputs(cfn, stack_name: str) -> dict[str, str]:
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _resources(cfn, stack_name: str) -> list[dict]:
    return [
        r
        for page in cfn.get_paginator("list_stack_resources").paginate(
            StackName=stack_name
        )
        for r in page["StackResourceSummaries"]
    ]


def _ai_stack_resource(cfn, stack_name: str, logical_id: str) -> str:
    ai_stack = next(
        r["PhysicalResourceId"]
        for r in _resources(cfn, stack_name)
        if r["LogicalResourceId"] == "AISTACK"
    )
    return next(
        r["PhysicalResourceId"]
        for r in _resources(cfn, ai_stack)
        if r["LogicalResourceId"] == logical_id
    )


def bus_name(stack_name: str, region: str | None = None) -> str | None:
    """The stack's meeting event bus, or None when EnableMeetingEvents is false."""
    cfn = boto3.Session(region_name=region).client("cloudformation")
    return _outputs(cfn, stack_name).get("MeetingEventBusName") or None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_probe(
    stack_name: str, region: str | None = None, timeout_s: float = 180.0
) -> dict[str, Any]:
    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    events = session.client("events")
    sqs = session.client("sqs")
    kinesis = session.client("kinesis")
    ddb = session.client("dynamodb")

    outputs = _outputs(cfn, stack_name)
    bus = outputs["MeetingEventBusName"]
    stream = outputs["CallDataStreamName"]
    vp_table = _ai_stack_resource(cfn, stack_name, "VirtualParticipantTable")

    suffix = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    call_id = f"integ-test-events-{suffix}"
    vp_id = f"integ-test-events-vp-{suffix}"
    owner = "integ-test@lma.aws"
    name = f"lma-integ-events-{suffix}"[:64]
    observed: dict[str, Any] = {
        "bus": bus,
        "call_id": call_id,
        "vp_id": vp_id,
        "rule": name,
        "queue_url": None,
        "vp_table": vp_table,
        "events": [],
    }

    # From here on every step can raise, and the fixture never receives a
    # result to clean up after; so whatever this run has made is removed
    # before the error goes on. ``cleanup`` skips what was never created.
    try:
        # The queue and the rule, before anything is published.
        queue_url = sqs.create_queue(
            QueueName=name, Attributes={"SqsManagedSseEnabled": "true"}
        )["QueueUrl"]
        observed["queue_url"] = queue_url
        queue_arn = sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        rule_arn = events.put_rule(
            Name=name,
            EventBusName=bus,
            EventPattern=json.dumps(
                {"source": ["lma"], "detail": {"meetingId": [call_id]}}
            ),
            State="ENABLED",
            Description="Temporary rule for the meeting events integration test",
        )["RuleArn"]
        sqs.set_queue_attributes(
            QueueUrl=queue_url,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "events.amazonaws.com"},
                                "Action": "sqs:SendMessage",
                                "Resource": queue_arn,
                                "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
                            }
                        ],
                    }
                )
            },
        )
        events.put_targets(
            Rule=name, EventBusName=bus, Targets=[{"Id": "queue", "Arn": queue_arn}]
        )
        # A new rule and target take a few seconds to start matching.
        time.sleep(15)

        def put(record: dict) -> None:
            kinesis.put_record(
                StreamName=stream,
                Data=json.dumps(record).encode("utf-8"),
                PartitionKey=call_id,
            )

        envelope = {
            "CallId": call_id,
            "AccessToken": "",
            "IdToken": "",
            "RefreshToken": "",
        }
        put(
            {
                **envelope,
                "EventType": "START",
                "CustomerPhoneNumber": "Integ Test Participant",
                "SystemPhoneNumber": "LMA Integ Test",
                "AgentId": owner,
                "CreatedAt": _now(),
                "Labels": [LABEL],
                "Audience": "internal",
            }
        )
        time.sleep(5)

        # The Virtual Participant: a new row, a status change, a write that leaves
        # the status alone, and a prompt for a person.
        def vp_write(status: str, **extra: Any) -> None:
            item = {
                "id": {"S": vp_id},
                "status": {"S": status},
                "owner": {"S": owner},
                "Owner": {"S": owner},
                "CallId": {"S": call_id},
                "isScheduled": {"BOOL": False},
                "meetingName": {
                    "S": "Integration meeting name that no event may carry"
                },
                "updatedAt": {"S": _now()},
                "ExpiresAfter": {"N": str(int(time.time()) + 86400)},
            }
            item.update(extra)
            ddb.put_item(TableName=vp_table, Item=item)

        vp_write("INITIALIZING")
        time.sleep(2)
        vp_write("JOINING")
        time.sleep(2)
        vp_write("JOINING", errorMessage={"S": "Waiting in the lobby"})
        time.sleep(2)
        vp_write(
            "MANUAL_ACTION_REQUIRED",
            manualActionType={"S": "CAPTCHA"},
            manualActionMessage={"S": "Solve the puzzle"},
        )

        put({**envelope, "EventType": "ADD_SUMMARY", "CallSummaryText": SUMMARY_TEXT})
        time.sleep(5)
        put({**envelope, "EventType": "END", "CreatedAt": _now()})

        deadline = time.monotonic() + timeout_s
        seen: list[dict] = []
        settling = False
        while time.monotonic() < deadline:
            response = sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10
            )
            for message in response.get("Messages", []):
                seen.append(json.loads(message["Body"]))
                sqs.delete_message(
                    QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"]
                )
            types = {event["detail-type"] for event in seen}
            vp_statuses = {
                event["detail"].get("vpStatus")
                for event in seen
                if event["detail-type"] == "Virtual Participant Status Changed"
            }
            complete = (
                set(DETAIL_TYPES) <= types and "MANUAL_ACTION_REQUIRED" in vp_statuses
            )
            if complete and not settling:
                # A little longer, so an event that should not have been published
                # has the chance to show up.
                settling = True
                deadline = min(deadline, time.monotonic() + 20)
        observed["events"] = seen
    except BaseException:
        observed["events"] = []
        cleanup(stack_name, observed, region=region)
        raise
    return observed


def cleanup(
    stack_name: str, observed: dict[str, Any], region: str | None = None
) -> None:
    """Best effort: every resource and row the probe made."""
    session = boto3.Session(region_name=region)
    events = session.client("events")
    sqs = session.client("sqs")
    ddb = session.client("dynamodb")
    for step in (
        lambda: events.remove_targets(
            Rule=observed["rule"], EventBusName=observed["bus"], Ids=["queue"]
        ),
        lambda: events.delete_rule(Name=observed["rule"], EventBusName=observed["bus"]),
        lambda: (
            observed["queue_url"] and sqs.delete_queue(QueueUrl=observed["queue_url"])
        ),
        lambda: ddb.delete_item(
            TableName=observed["vp_table"], Key={"id": {"S": observed["vp_id"]}}
        ),
    ):
        try:
            step()
        except Exception:  # noqa: BLE001 - best effort
            pass
    try:
        from kds_pipeline_probe import cleanup as delete_meeting

        delete_meeting(stack_name, observed["call_id"], region=region)
    except Exception:  # noqa: BLE001 - best effort
        pass


if __name__ == "__main__":
    stack = sys.argv[1] if len(sys.argv) > 1 else "LMA"
    if not bus_name(stack):
        print(
            "no MeetingEventBusName output: EnableMeetingEvents is false on this stack"
        )
        sys.exit(2)
    result = run_probe(stack)
    try:
        for event in result["events"]:
            print(event["detail-type"], json.dumps(event["detail"]))
        missing = set(DETAIL_TYPES) - {e["detail-type"] for e in result["events"]}
        print("MISSING:", sorted(missing) if missing else "none")
    finally:
        cleanup(stack, result)
    sys.exit(1 if missing else 0)
