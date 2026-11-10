#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""AppSync resolver for the troubleshooting agent, and the worker that runs it.

A diagnosis takes minutes -- the agent's own wall-clock budget is 240 seconds
by default -- and an AppSync request is answered within 30, so a diagnosis is a
job. This one function serves both halves:

``Mutation.startTroubleshooting(input)``
    Decides whether the caller may ask, records a PENDING job, starts the
    worker with an asynchronous invocation of this same function, and returns
    the job at once.

``Query.getTroubleshootingJob(JobId)``
    The job, to the user who started it. The page reads it every few seconds
    until it is COMPLETED or FAILED.

The worker (an event carrying ``troubleshootingJob`` and no ``info``, which an
AppSync request never produces because ``troubleshootingLambda.js`` builds its
payload from ``arguments``, ``identity`` and ``info``) reads the job from the
table, invokes the troubleshooting agent synchronously, and writes the answer
back. The worker takes only the job id from its event: what to ask and what is
in scope come from the row the resolver wrote after its access check.

Who may ask: the Admin group, for every diagnosis -- of the whole deployment,
of a meeting, or of a Virtual Participant. A meeting or Virtual Participant
named in the request is read before the job is recorded, and a request naming
one that does not exist is refused. The Admin's question is asked as written;
with none, a scoped diagnosis asks the fixed question for the resource in scope
(``scoped_question``) and an unscoped one asks for the most recent failure.

A job is returned only to the user who started it.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

import boto3
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
# The agent is invoked synchronously and can run for its whole 300-second
# Timeout, so the read timeout is longer than that. No retries at all: a retried
# invocation is a second diagnosis from the start, paid for again.
AGENT_INVOKE_CONFIG = BotoCoreConfig(
    connect_timeout=10,
    read_timeout=int(os.environ.get("AGENT_READ_TIMEOUT_SECONDS", "330")),
    retries={"total_max_attempts": 1},
)
DYNAMODB = boto3.client("dynamodb", config=CLIENT_CONFIG)
LAMBDA = boto3.client("lambda", config=CLIENT_CONFIG)
AGENT_LAMBDA = boto3.client("lambda", config=AGENT_INVOKE_CONFIG)

JOBS_TABLE = os.environ.get("TROUBLESHOOTING_JOBS_TABLE", "")
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
VIRTUAL_PARTICIPANT_TABLE = os.environ.get("VIRTUAL_PARTICIPANT_TABLE", "")
AGENT_FUNCTION_ARN = os.environ.get("TROUBLESHOOTING_AGENT_FUNCTION_ARN", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

#: Days a job, and its answer, are kept before DynamoDB's TTL removes it.
JOB_TTL_DAYS = 7
#: A job still PENDING or RUNNING this long after it last changed is reported as
#: FAILED: its worker has stopped (the function's Timeout is 360 seconds and an
#: asynchronous invocation of it is not retried), so it will never finish.
STALE_AFTER_SECONDS = 420
MAX_QUESTION_LENGTH = 2000
MAX_ID_LENGTH = 256

START_FIELD = "startTroubleshooting"
GET_FIELD = "getTroubleshootingJob"
WORKER_KEY = "troubleshootingJob"

PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

STACK_WIDE_DEFAULT_QUESTION = "Diagnose the most recent failure in this deployment."


class ForbiddenError(Exception):
    """The caller may not ask this, or see this job."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _caller(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Who is asking, from the AppSync ``identity`` block only.

    ``identity.username`` is the value ``getCall`` and the Virtual Participant
    resolvers compare Owner and SharedWith with.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    return {
        "username": str(identity.get("username") or ""),
        "subject": str(claims.get("sub") or identity.get("sub") or ""),
        "is_admin": ADMIN_GROUP in groups,
    }


# BEGIN shared_with_entries -- one text in every Lambda that reads SharedWith;
# lma-ai-stack/test/test_shared_with_access.py holds the copies to it.
# The entries of a SharedWith value, read as the AppSync resolvers read them
# (the "SharedWith entries" VTL block and sharedWithEntries in the JS
# resolvers): a list, tuple or set holds one entry per string element, and a
# string -- comma-separated or "[a, b]" -- is split at every "[", "]" and ",".
# Those three characters only ever separate entries, so a list element that
# contains one is not an entry. Entries are trimmed and empty ones dropped;
# membership is then a comparison of whole entries.
def shared_with_entries(value: object) -> list:
    """The trimmed, non-empty entries of a SharedWith value."""
    if isinstance(value, str):
        parts = value.replace("[", ",").replace("]", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [
            part
            for part in value
            if isinstance(part, str) and not any(separator in part for separator in "[],")
        ]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


# END shared_with_entries


def _owner_or_shared(item: Mapping[str, Any], username: str) -> bool:
    """Whether ``username`` is the row's Owner or a whole entry of its SharedWith."""
    owner = (item.get("Owner") or {}).get("S", "")
    shared_raw = item.get("SharedWith") or {}
    shared = shared_with_entries(
        shared_raw.get("S") or shared_raw.get("SS") or [v.get("S") for v in shared_raw.get("L", [])]
    )
    return bool(username) and (owner == username or username in shared)


def require_call_access(caller: Mapping[str, Any], call_id: str) -> None:
    """The meeting must exist and pass the ``getCall`` rule.

    Every caller reaching this is an Admin (``start_troubleshooting`` checks the
    group first); the owner and SharedWith terms keep the rule the same as
    ``getCall``'s.
    """
    item = (
        DYNAMODB.get_item(
            TableName=EVENT_SOURCING_TABLE,
            Key={"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}},
            ProjectionExpression="#o, SharedWith",
            ExpressionAttributeNames={"#o": "Owner"},
        ).get("Item")
        or {}
    )
    if item and (caller["is_admin"] or _owner_or_shared(item, caller["username"])):
        return
    # One answer whether the meeting is missing or not readable by the caller.
    LOGGER.info("refused a diagnosis of a meeting to %s", caller["subject"] or "(none)")
    raise ForbiddenError("Unauthorized: you do not have access to this meeting")


def require_vp_access(caller: Mapping[str, Any], vp_id: str) -> None:
    """The Virtual Participant must exist and pass the ``getVirtualParticipant`` rule.

    As for meetings, every caller reaching this is an Admin.
    """
    item = (
        DYNAMODB.get_item(
            TableName=VIRTUAL_PARTICIPANT_TABLE,
            Key={"id": {"S": vp_id}},
            ProjectionExpression="#o, SharedWith",
            ExpressionAttributeNames={"#o": "Owner"},
        ).get("Item")
        or {}
    )
    if item and (caller["is_admin"] or _owner_or_shared(item, caller["username"])):
        return
    LOGGER.info("refused a diagnosis of a virtual participant to %s", caller["subject"] or "(none)")
    raise ForbiddenError("Unauthorized: you do not have access to this Virtual Participant")


def _require_admin(caller: Mapping[str, Any]) -> None:
    if not caller["is_admin"]:
        raise ForbiddenError(
            f"Unauthorized: only members of the {ADMIN_GROUP} group can run a diagnosis"
        )


def scoped_question(call_id: str, vp_id: str) -> str:
    """The question a diagnosis of one resource asks."""
    if vp_id and call_id:
        return (
            f"Diagnose Virtual Participant {vp_id} and meeting {call_id}: did the Virtual "
            "Participant join, was the meeting transcribed, and if anything failed or ended "
            "early, why?"
        )
    if vp_id:
        return (
            f"Diagnose Virtual Participant {vp_id}: did it join the meeting, and if it "
            "failed or ended early, why?"
        )
    return (
        f"Diagnose meeting {call_id}: was it captured and transcribed from start to end, "
        "and if anything failed, why?"
    )


def _clean_id(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_ID_LENGTH:
        raise ValueError(f"{name} is longer than {MAX_ID_LENGTH} characters")
    return text


def _job_shape(item: Mapping[str, Any]) -> Dict[str, Any]:
    """A job row in the TroubleshootingJob GraphQL shape."""

    def text(name: str) -> Optional[str]:
        value = (item.get(name) or {}).get("S")
        return value if value else None

    truncated = (item.get("Truncated") or {}).get("BOOL")
    return {
        "JobId": text("JobId"),
        "Status": text("Status") or FAILED,
        "Question": text("Question"),
        "CallId": text("CallId"),
        "VirtualParticipantId": text("VirtualParticipantId"),
        "Answer": text("Answer"),
        "Error": text("Error"),
        "Truncated": bool(truncated) if truncated is not None else None,
        "CreatedAt": text("CreatedAt"),
        "UpdatedAt": text("UpdatedAt"),
    }


def start_troubleshooting(input_data: Mapping[str, Any], caller: Mapping[str, Any]) -> Dict:
    """Authorize, record a PENDING job, and start the worker."""
    if not caller["username"]:
        raise ForbiddenError("Unauthorized: a signed-in user is required")
    call_id = _clean_id(input_data.get("CallId"), "CallId")
    vp_id = _clean_id(input_data.get("VirtualParticipantId"), "VirtualParticipantId")

    _require_admin(caller)
    if call_id:
        require_call_access(caller, call_id)
    if vp_id:
        require_vp_access(caller, vp_id)

    if not AGENT_FUNCTION_ARN:
        raise ValueError("The troubleshooting agent is not deployed in this stack")

    asked = str(input_data.get("Question") or "").strip()
    if asked:
        if len(asked) > MAX_QUESTION_LENGTH:
            raise ValueError(f"Question is longer than {MAX_QUESTION_LENGTH} characters")
        question = asked
    elif call_id or vp_id:
        question = scoped_question(call_id, vp_id)
    else:
        question = STACK_WIDE_DEFAULT_QUESTION

    now = _now()
    job_id = str(uuid.uuid4())
    item: Dict[str, Any] = {
        "JobId": {"S": job_id},
        "Requester": {"S": caller["username"]},
        "Status": {"S": PENDING},
        "Question": {"S": question},
        "CreatedAt": {"S": _iso(now)},
        "UpdatedAt": {"S": _iso(now)},
        "ExpiresAt": {"N": str(int(now.timestamp()) + JOB_TTL_DAYS * 86400)},
    }
    if call_id:
        item["CallId"] = {"S": call_id}
    if vp_id:
        item["VirtualParticipantId"] = {"S": vp_id}
    DYNAMODB.put_item(
        TableName=JOBS_TABLE, Item=item, ConditionExpression="attribute_not_exists(JobId)"
    )

    try:
        LAMBDA.invoke(
            FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
            InvocationType="Event",
            Payload=json.dumps({WORKER_KEY: {"JobId": job_id}}).encode("utf-8"),
        )
    except (ClientError, KeyError):
        LOGGER.exception("could not start the troubleshooting worker")
        _finish(job_id, FAILED, error="The diagnosis could not be started.")
        raise
    LOGGER.info(
        "started troubleshooting job %s for %s (call=%s vp=%s)",
        job_id,
        caller["subject"] or "(none)",
        bool(call_id),
        bool(vp_id),
    )
    return _job_shape(item)


def get_troubleshooting_job(job_id: str, caller: Mapping[str, Any]) -> Dict[str, Any]:
    """The job, to the user who started it."""
    item = (
        DYNAMODB.get_item(
            TableName=JOBS_TABLE, Key={"JobId": {"S": job_id}}, ConsistentRead=True
        ).get("Item")
        or {}
    )
    requester = (item.get("Requester") or {}).get("S", "")
    if not item or not caller["username"] or requester != caller["username"]:
        raise ForbiddenError("Unauthorized: no such troubleshooting job")
    job = _job_shape(item)
    if job["Status"] in (PENDING, RUNNING) and _is_stale(job.get("UpdatedAt")):
        job["Status"] = FAILED
        job["Error"] = "The diagnosis did not finish. Run it again."
    return job


def _is_stale(updated_at: Optional[str]) -> bool:
    if not updated_at:
        return True
    try:
        moment = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (_now() - moment).total_seconds() > STALE_AFTER_SECONDS


# ── worker ──────────────────────────────────────────────────────────────────


def _finish(
    job_id: str,
    status: str,
    answer: Optional[str] = None,
    error: Optional[str] = None,
    truncated: Optional[bool] = None,
) -> None:
    names = {"#s": "Status", "#u": "UpdatedAt"}
    values: Dict[str, Any] = {":s": {"S": status}, ":u": {"S": _iso(_now())}}
    sets = ["#s = :s", "#u = :u"]
    for attribute, value in (("Answer", answer), ("Error", error)):
        if value:
            names[f"#{attribute}"] = attribute
            values[f":{attribute}"] = {"S": value}
            sets.append(f"#{attribute} = :{attribute}")
    if truncated is not None:
        names["#t"] = "Truncated"
        values[":t"] = {"BOOL": bool(truncated)}
        sets.append("#t = :t")
    DYNAMODB.update_item(
        TableName=JOBS_TABLE,
        Key={"JobId": {"S": job_id}},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _claim(job_id: str) -> Optional[Dict[str, Any]]:
    """Move the job from PENDING to RUNNING, or None if it is not PENDING or is stale."""
    try:
        return DYNAMODB.update_item(
            TableName=JOBS_TABLE,
            Key={"JobId": {"S": job_id}},
            UpdateExpression="SET #s = :running, #u = :u",
            # A job the page already reads as failed (stale) is not started.
            ConditionExpression="#s = :pending AND #u > :fresh",
            ExpressionAttributeNames={"#s": "Status", "#u": "UpdatedAt"},
            ExpressionAttributeValues={
                ":running": {"S": RUNNING},
                ":pending": {"S": PENDING},
                ":u": {"S": _iso(_now())},
                ":fresh": {"S": _iso(_now() - timedelta(seconds=STALE_AFTER_SECONDS))},
            },
            ReturnValues="ALL_NEW",
        ).get("Attributes")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


def agent_payload(item: Mapping[str, Any]) -> Dict[str, str]:
    """The agent's event, from the stored job only.

    No ``messageId``: the agent streams its progress only when one is given, and
    the page reads the finished job instead.
    """
    payload = {"query": (item.get("Question") or {}).get("S", "")}
    call_id = (item.get("CallId") or {}).get("S")
    vp_id = (item.get("VirtualParticipantId") or {}).get("S")
    if call_id:
        payload["callId"] = call_id
    if vp_id:
        payload["vpId"] = vp_id
    return payload


def interpret_agent_response(response: Mapping[str, Any]) -> Dict[str, Any]:
    """The job's outcome from the agent's invocation response.

    The agent answers ``{"statusCode", "body"}``; an error body carries a fixed
    message and an exception type, never the exception's text, so its ``error``
    is safe to show. A function error (a timeout, or the runtime failing) has
    no such body and is reported with a fixed message of this function's own.
    """
    raw = response["Payload"].read()
    if response.get("FunctionError"):
        LOGGER.error("the troubleshooting agent failed: %s", raw[:2000])
        return {"status": FAILED, "error": "The diagnosis could not be completed."}
    try:
        outer = json.loads(raw)
        body = json.loads(outer.get("body") or "{}")
    except (ValueError, AttributeError):
        LOGGER.error("the troubleshooting agent returned an unreadable response")
        return {"status": FAILED, "error": "The diagnosis returned an unreadable response."}
    if outer.get("statusCode") == 200 and body.get("answer"):
        return {
            "status": COMPLETED,
            "answer": str(body["answer"]),
            "truncated": bool(body.get("truncated")),
        }
    return {
        "status": FAILED,
        "error": str(body.get("error") or "The diagnosis could not be completed."),
    }


def run_worker(job_id: str) -> None:
    """Run one job's diagnosis and record its outcome."""
    if not job_id:
        LOGGER.error("a worker event named no job")
        return
    item = _claim(job_id)
    if item is None:
        LOGGER.info("troubleshooting job %s is not pending; nothing to do", job_id)
        return
    started = time.monotonic()
    try:
        response = AGENT_LAMBDA.invoke(
            FunctionName=AGENT_FUNCTION_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps(agent_payload(item)).encode("utf-8"),
        )
        outcome = interpret_agent_response(response)
    except Exception:  # noqa: BLE001 - the job must reach a final state
        LOGGER.exception("could not invoke the troubleshooting agent")
        outcome = {"status": FAILED, "error": "The diagnosis could not be completed."}
    _finish(
        job_id,
        outcome["status"],
        answer=outcome.get("answer"),
        error=outcome.get("error"),
        truncated=outcome.get("truncated"),
    )
    LOGGER.info(
        "troubleshooting job %s finished %s in %.1fs",
        job_id,
        outcome["status"],
        time.monotonic() - started,
    )


def handler(event: Dict[str, Any], context: Any) -> Optional[Dict[str, Any]]:
    """AppSync entry point, and the worker's."""
    event = event or {}
    if WORKER_KEY in event and "info" not in event:
        run_worker(str((event.get(WORKER_KEY) or {}).get("JobId") or ""))
        return None

    try:
        return _resolve(event)
    except (ForbiddenError, ValueError):
        # Messages written here, for the caller.
        raise
    except Exception as error:  # pylint: disable=broad-except
        # Anything else (an AWS error, say) is logged, and the caller gets a
        # fixed message rather than the error's own text.
        LOGGER.exception("troubleshooting request failed")
        raise RuntimeError(
            "The diagnosis service could not complete the request. Try again."
        ) from error


def _resolve(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    field = ((event.get("info") or {}).get("fieldName") or "").strip()
    caller = _caller(event)
    arguments = event.get("arguments") or {}
    if field == START_FIELD:
        return start_troubleshooting(arguments.get("input") or {}, caller)
    if field == GET_FIELD:
        job_id = _clean_id(arguments.get("JobId"), "JobId")
        if not job_id:
            raise ValueError("JobId is required")
        return get_troubleshooting_job(job_id, caller)
    raise ValueError(f"this resolver does not serve {field or '(nothing)'}")
