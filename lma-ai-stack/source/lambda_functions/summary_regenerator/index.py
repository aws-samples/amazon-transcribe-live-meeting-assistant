# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Re-run a meeting's summary on demand, and keep the stored one if the run fails.

A summary is generated once, when the meeting ends. It goes stale: an admin
edits the summary prompt templates, or somebody names the separated voices on the
Speakers panel so the transcript now reads "Sarah Lee" where it read `spk_0`.
This function re-runs summarisation against the transcript and the prompt
templates as they are now, and replaces the stored summary with the result.

Reachable two ways, which are the two halves of one run:

* **The meeting page's Regenerate button**, through the `regenerateCallSummary`
  AppSync mutation. The caller's own identity is checked here, because a Lambda
  resolver has no DynamoDB condition to lean on. This leg records that a run has
  started and returns straight away -- summarisation calls a model once per
  prompt template and routinely takes longer than the 30 seconds AppSync allows
  a resolver.
* **Itself**, invoked asynchronously by that first leg, with no identity. That
  leg does the work and writes the result, which reaches every open meeting page
  through `onUpdateCall` -- the same route a summary from the Kinesis pipeline
  takes.

Summarisation itself is NOT reimplemented here. The configured summary function
(`TRANSCRIPT_SUMMARY_FUNCTION_ARN`, which is the Bedrock summariser or a
customer's own end-of-call hook) is invoked with only the `CallId` the hook
contract requires (see docs/transcript-summarization.md). The end-of-meeting
orchestrator forwards its whole END event, of which the `CallId` is the only
field the summariser reads; the other field it would accept is a `Prompt`
override, and passing one would both bypass the stored prompt templates and --
in the Bedrock summariser -- skip the snapshot of the transcript and summary
written to S3 for the Bedrock Knowledge Base. Refreshing that snapshot is one of
the reasons to regenerate, so no override is ever passed.

A failed run must leave the previous summary standing: a summary that was there
and is now "An error occurred." is worse than a stale one. So the text is
written only once a usable result is in hand, and the failure is recorded in the
small state object beside it instead. A run that the meeting outlived is
discarded for the same reason: it summarised part of a transcript the
end-of-meeting summary covers in full.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
import requests
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config

# Standard logging rather than Powertools: the layer that carries Powertools is
# built for arm64 only, and this function has no other reason to be pinned to an
# architecture. Same choice as FetchTranscript and the speaker name resolver.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

EVENT_SOURCING_TABLE = os.environ["EVENT_SOURCING_TABLE"]
APPSYNC_GRAPHQL_URL = os.environ["APPSYNC_GRAPHQL_URL"]
# Empty in a deployment with no summariser configured -- the LAMBDA option with
# no hook ARN supplied. Reported to the caller rather than raising.
TRANSCRIPT_SUMMARY_FUNCTION_ARN = os.environ.get("TRANSCRIPT_SUMMARY_FUNCTION_ARN", "")

# How long a run may be in flight before another is allowed to start. The
# meeting is marked as regenerating while a run is going, which is what the
# button reads to disable itself in every open page; without an expiry a run
# whose Lambda was killed would leave the button disabled for ever. Comfortably
# longer than the summary function's own 900s ceiling.
STALE_RUN_SECONDS = int(os.environ.get("SUMMARY_REGENERATION_STALE_SECONDS", "1200"))

# How far ahead of now a run's start stamp may be and still be believed. The
# stamp is written by this function from its own clock and read back by it and by
# the browser, so a small disagreement between the two is ordinary. Anything
# further ahead is not a clock, and is not believed: a stamp dated hours from now
# would otherwise mark the meeting as regenerating until that hour arrived. Must
# match CLOCK_SKEW_ALLOWANCE_MS in ui/src/components/common/summary-state.js.
CLOCK_SKEW_ALLOWANCE_SECONDS = 60

# Storing a result: how many attempts, how long each waits for an answer, and
# the base of the backoff between them (doubling, so 1s then 2s). See
# write_summary for why this is retried here rather than by Lambda. Named rather
# than inlined because the worst case they add up to has to fit inside this
# function's own timeout alongside the wait for the summariser -- there is a test
# for that sum.
APPSYNC_WRITE_ATTEMPTS = 3
APPSYNC_WRITE_TIMEOUT_SECONDS = 20
APPSYNC_WRITE_BACKOFF_SECONDS = 1

# What the configured summary function hands back when it could not produce a
# summary. The Bedrock summariser catches everything and returns this rather
# than raising, so the string is the only signal there is that the run failed.
# Compared case-insensitively after stripping.
UNUSABLE_SUMMARIES = frozenset(
    {
        "",
        "an error occurred.",
        "an error occurred",
        "no summary available",
    }
)

# The meeting Status the pipeline writes when a meeting ends, after which it
# produces the end-of-meeting summary. Same literal the Kinesis consumer uses.
STATUS_MEETING_ENDED = "ENDED"

SOURCE_GENERATED = "generated"
SOURCE_MANUAL = "manual"
STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
STATUS_IDLE = "idle"

DESERIALIZER = TypeDeserializer()

dynamodb = boto3.client("dynamodb")
lambda_client = boto3.client(
    "lambda",
    config=Config(
        # Deliberately SHORTER than the summary function's own ceiling. Both
        # functions stop at Lambda's 900s maximum, so waiting the summariser out
        # in full is not possible: this one would be killed mid-wait, having
        # written nothing, leaving the meeting marked as regenerating until that
        # mark went stale. Giving up with time in hand instead means a stuck
        # summariser produces a reported failure. Safe to abandon only because
        # max_attempts is 1 -- the orchestrator's longer socket timeout for the
        # speaker-naming invoke exists to stop a retry re-running the callee,
        # and there are no retries here to stop.
        read_timeout=int(os.environ.get("SUMMARY_READ_TIMEOUT", "780")),
        retries={"mode": "standard", "max_attempts": 1},
    ),
)
# Its own client: the self-invoke is a fire-and-forget Event call that must not
# inherit the long socket timeout above. One attempt only -- a socket timeout on
# a request that did in fact succeed would queue the run a second time, and the
# second leg has no single-flight check of its own. The caller is told the
# request failed instead, and can press the button again.
starter_client = boto3.client(
    "lambda", config=Config(retries={"mode": "standard", "max_attempts": 1})
)

# The session used to sign the writes back to AppSync. At module scope so that
# a test can replace it with one carrying static credentials (see
# `static_signing_credentials` in test_index.py): signing must resolve its
# credentials from this one object, not from whatever ambient chain the process
# happens to have, which a test cannot control and a build runner does not have.
signing_session = boto3.Session()


def _deserialize(item):
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}


def read_meeting(call_id):
    """The meeting row, which carries the summary state and the access fields.

    Read consistently: `start` decides from this whether a run is already going,
    and an eventually consistent read could miss a mark written moments ago by
    the press it is meant to notice.
    """
    response = dynamodb.get_item(
        TableName=EVENT_SOURCING_TABLE,
        Key={"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return _deserialize(item) if item else None


def caller_may_edit(meeting, identity):
    """The same rule the read path and the direct summary write both apply.

    A Lambda resolver cannot express this as a DynamoDB condition, so it is
    checked here instead: Admin, the meeting's owner, or a user it has been
    shared with. An invocation with no Cognito identity is the backend calling
    itself (this function's own second leg) and is allowed.

    `SharedWith` is read here in Python, so the share list is compared entry by
    entry in both shapes it is stored in -- the DynamoDB List `shareCall` writes
    and the comma-separated String on older rows. The templates that express the
    same rule as a DynamoDB condition use `contains`, which is a whole-element
    test on the List and a substring test on the String, because a condition
    expression cannot split a string; this path is the exact one.

    One difference from the templates is deliberate: a caller with no
    `username` falls back to `sub` here, where a VTL condition matches on
    `username` alone. An AppSync Cognito caller always carries a username, so
    the fallback is a belt for a shape that does not arrive rather than a
    second way in.
    """
    if not identity:
        return True
    username = identity.get("username") or identity.get("sub")
    groups = identity.get("groups") or []
    if "Admin" in groups:
        return True
    # Only a named caller can match one of the fields below. A meeting row
    # carries no Owner attribute until someone owns it, and absent reads as
    # None, so every comparison needs a name on both sides to mean anything.
    if not username:
        return False
    if meeting.get("Owner") == username:
        return True
    shared_with = meeting.get("SharedWith")
    if isinstance(shared_with, (list, set)):
        return username in shared_with
    if isinstance(shared_with, str):
        # Whole entries, not a substring of the joined string: the older
        # comma-separated form holds one username per element, and membership is
        # a property of the elements.
        return username in {entry.strip() for entry in shared_with.split(",")}
    return False


def parse_state(raw):
    """The summary's state object, degrading to empty rather than raising.

    Absent on every meeting summarised before this field existed, and on every
    meeting whose summary came from the Kinesis pipeline, which does not write
    it. Both read as a generated summary with nothing running.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_running(state, now):
    """True when a run started recently enough to still be in flight.

    Only a stamp inside the window [now - STALE_RUN_SECONDS, now + CLOCK_SKEW]
    counts. An older one is treated as finished however the run was left, so a
    run whose Lambda was killed cannot block the meeting for ever; a stamp
    further ahead than the skew allowance, and a mark with no readable stamp at
    all, are treated the same way, because neither is a window this can ever
    read its way out of.

    This is a guard against a second click, not a lock: the stamp is read and
    then written, so two callers within the same instant can both pass. The cost
    of that is one extra set of model calls and a last-write-wins on the text,
    not a corrupt summary. The same last-write-wins applies to a hand edit saved
    while a run is going: the run writes its result when it finishes, so the
    edit is replaced. The panel's confirmation covers the ordinary case -- it is
    read before a run starts -- and not an edit that arrives mid-run from a page
    that had not yet seen the mark. Bounding either properly needs a conditional
    write per caller, which buys little for a button a handful of people press.
    """
    if state.get("status") != STATUS_RUNNING:
        return False
    # `updateCallSummaryText` stores whatever state string it is given, so the
    # shape of this field is not something a reader can take for granted: a mark
    # with no usable stamp reads as finished rather than blocking the meeting
    # with nothing able to clear it.
    try:
        started = datetime.fromisoformat(state.get("startedAt"))
    except (TypeError, ValueError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if started > now + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_SECONDS):
        # Dated in the future: a stamp this side of the skew allowance is a
        # clock disagreeing, and beyond it is a value nothing here wrote, which
        # would otherwise hold the meeting until that time arrived.
        return False
    return started > now - timedelta(seconds=STALE_RUN_SECONDS)


def summary_is_usable(summary):
    """Whether this is a summary worth replacing the stored one with.

    The configured summary function never raises -- the Bedrock summariser
    catches everything and returns a fixed string -- so this is the only place
    a failed run can be recognised.
    """
    if not isinstance(summary, str):
        return False
    return summary.strip().lower() not in UNUSABLE_SUMMARIES


# Must select every field onUpdateCall selects. AppSync publishes a mutation's
# own response to that subscription's subscribers, and a field the subscription
# selects that the mutation did not arrives as null -- so a two-field payload
# would blank the meeting's status, timestamps and names in every open page.
_CALL_FIELDS = """
      PK
      SK
      CallId
      AgentId
      CallCategories
      IssuesDetected
      CallSummaryText
      CallSummaryState
      SpeakerIdentities
      Status
      UpdatedAt
      CreatedAt
      CustomerPhoneNumber
      SystemPhoneNumber
      RecordingUrl
      VideoRecordingUrl
      PcaUrl
      Owner
      SharedWith
      TotalConversationDurationMillis
      Sentiment {
        OverallSentiment { AGENT CALLER }
        SentimentByPeriod {
          QUARTER {
            AGENT { BeginOffsetMillis EndOffsetMillis Score }
            CALLER { BeginOffsetMillis EndOffsetMillis Score }
          }
        }
      }
"""


def write_summary(call_id, text=None, state=None):
    """Store the text and/or the state through AppSync.

    Through AppSync rather than straight to DynamoDB so that `onUpdateCall`
    publishes: every open meeting page then shows the new summary, or the
    failure, without being reloaded.

    Retried here rather than left to the asynchronous invoke's own retries,
    which would re-run the whole summarisation -- a second and third set of
    model calls for a summary already computed and sitting in this frame. A
    GraphQL error is not retried: a refused write would be refused again.
    """
    query = (
        "mutation UpdateCallSummaryText($input: UpdateCallSummaryTextInput!) {\n"
        "  updateCallSummaryText(input: $input) {\n" + _CALL_FIELDS + "  }\n}"
    )
    variables = {"input": {"CallId": call_id}}
    if text is not None:
        variables["input"]["CallSummaryText"] = text
    if state is not None:
        variables["input"]["CallSummaryState"] = json.dumps(state)
    payload = json.dumps({"query": query, "variables": variables})
    last_error = None
    for attempt in range(APPSYNC_WRITE_ATTEMPTS):
        # Signed inside the loop so a retry does not carry a signature from
        # several seconds ago.
        request = AWSRequest(
            method="POST",
            url=APPSYNC_GRAPHQL_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(
            signing_session.get_credentials(), "appsync", signing_session.region_name
        ).add_auth(request)
        try:
            response = requests.post(
                APPSYNC_GRAPHQL_URL,
                data=payload,
                headers=dict(request.headers),
                timeout=APPSYNC_WRITE_TIMEOUT_SECONDS,
            )
            body = response.json()
        except Exception as error:  # noqa: BLE001 - retried, then re-raised below
            last_error = error
        else:
            if body.get("errors"):
                raise RuntimeError(f"updateCallSummaryText failed: {body['errors']}")
            return body
        if attempt + 1 < APPSYNC_WRITE_ATTEMPTS:
            LOGGER.warning("Could not reach AppSync to store the summary; retrying")
            time.sleep(APPSYNC_WRITE_BACKOFF_SECONDS * 2**attempt)
    raise RuntimeError(f"updateCallSummaryText could not be reached: {last_error}")


def invoke_summary_function(call_id):
    """Run the configured summariser over the meeting's current transcript.

    A CallId and a request to re-read configuration, and nothing else. That is
    what makes this a re-run of the same path the end-of-meeting summary takes:
    the summariser fetches the transcript itself (with whatever speaker names
    are stored now), reads the prompt templates itself, and -- in the Bedrock
    summariser -- refreshes the Knowledge Base snapshot, which it only does when
    no prompt override is given.

    `RefreshConfiguration` is why pressing Regenerate right after saving a
    prompt uses the prompt that was just saved. The summariser caches the prompt
    templates for a few minutes, which is right for the end-of-meeting runs it
    does unattended, and wrong for a button somebody pressed on purpose. A
    summariser that does not recognise the field ignores it, so the customer's
    own function under the LAMBDA option needs no change.
    """
    response = lambda_client.invoke(
        FunctionName=TRANSCRIPT_SUMMARY_FUNCTION_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps({"CallId": call_id, "RefreshConfiguration": True}),
    )
    body = json.loads(response["Payload"].read().decode("utf-8"))
    if not isinstance(body, dict):
        raise RuntimeError("the summary function did not return a JSON object")
    return body.get("summary")


def start(call_id, identity, self_arn):
    """Accept a regeneration request and hand the work to the second leg."""
    meeting = read_meeting(call_id)
    if meeting is None:
        return {"Success": False, "Message": "no such meeting"}
    if not caller_may_edit(meeting, identity):
        return {"Success": False, "Message": "not permitted"}
    if not TRANSCRIPT_SUMMARY_FUNCTION_ARN:
        return {"Success": False, "Message": "summarisation is not configured for this deployment"}

    now = datetime.now(timezone.utc)
    state = parse_state(meeting.get("CallSummaryState"))
    if is_running(state, now):
        return {"Success": False, "Message": "a summary is already being regenerated"}

    # Queue the work BEFORE marking the meeting as regenerating. Marking first
    # and failing to queue would leave the meeting looking busy, and the button
    # disabled everywhere, until the mark went stale.
    #
    # The ordering costs one narrow window in exchange: a second leg that fails
    # at once -- the summariser invoke rejected outright -- can write its
    # `failed` state before the mark below lands, and the mark then overwrites
    # it, so the meeting reads as regenerating until the stale cut-off rather
    # than showing the failure. Closing it needs a conditional write per caller,
    # the same machinery `is_running` deliberately goes without; the two
    # outcomes are a hidden failure for twenty minutes against a button disabled
    # for twenty minutes, and neither loses the stored summary.
    starter_client.invoke(
        FunctionName=self_arn,
        InvocationType="Event",
        Payload=json.dumps({"CallId": call_id, "Trigger": "regenerate"}),
    )
    try:
        # The previous source is carried through so that, if the run fails, the
        # meeting still reads as hand-edited where it was hand-edited.
        write_summary(
            call_id,
            state={
                "source": state.get("source") or SOURCE_GENERATED,
                "status": STATUS_RUNNING,
                "startedAt": now.isoformat(),
            },
        )
    except Exception:  # pylint: disable=broad-except
        # The run is already queued, so reporting a failure here would be
        # wrong. What is lost is the progress shown while it goes: the pressing
        # page shows its own for REGENERATE_HANDOVER_MS (30s in CallPanel.jsx),
        # and after that no page shows the run until its result lands.
        LOGGER.exception("Could not mark %s as regenerating; the run continues", call_id)
    return {"Success": True, "Message": "regenerating the summary"}


def run(call_id):
    """Do the summarisation and write whatever came of it."""
    now = datetime.now(timezone.utc).isoformat()
    # Re-read rather than trusting what the first leg saw: the two legs are
    # separate invocations, so an edit saved in between would make the first
    # leg's `source` out of date. It is only the label that this preserves --
    # a successful run replaces the text whatever the label says, which is what
    # regenerating means, and `is_running` records the one case the panel's
    # confirmation does not cover.
    meeting = read_meeting(call_id) or {}
    state = parse_state(meeting.get("CallSummaryState"))
    previous_source = state.get("source") or SOURCE_GENERATED
    was_live = meeting.get("Status") != STATUS_MEETING_ENDED

    try:
        summary = invoke_summary_function(call_id)
    except Exception:  # noqa: BLE001 - recorded for the user, see below
        # Logged in full here; what reaches the browser is the sentence written
        # into the state below, rather than Lambda and model-service internals.
        LOGGER.exception("Summary regeneration failed for %s", call_id)
        summary = None

    if not summary_is_usable(summary):
        LOGGER.warning("Keeping the stored summary for %s: the run produced nothing", call_id)
        # No CallSummaryText in this write. That is the whole point: the stored
        # summary survives a failed run.
        write_summary(
            call_id,
            state={
                "source": previous_source,
                "status": STATUS_FAILED,
                "message": "could not regenerate the summary just now",
                "updatedAt": now,
            },
        )
        return {"ok": False}

    # A run started while the meeting was still going summarised the transcript
    # as it stood then. If the meeting has since ended, the pipeline is writing
    # its own summary from the whole transcript, and this one -- built from part
    # of it -- must not land on top of that. The two writes are unordered, so
    # which arrived first says nothing; this is dropped either way.
    #
    # What this does NOT undo is the Knowledge Base snapshot. Under the BEDROCK
    # option the summariser writes that copy to S3 itself, inside the
    # invoke_summary_function call above, so by the time this decision is made
    # the snapshot already holds what this run saw. Pressing Regenerate once the
    # meeting has ended writes it again from the whole transcript, which is what
    # the docs tell the user to do.
    if was_live and (read_meeting(call_id) or {}).get("Status") == STATUS_MEETING_ENDED:
        LOGGER.info(
            "Discarding the regenerated summary for %s: the meeting ended while it ran, "
            "so the end-of-meeting summary covers more of the transcript",
            call_id,
        )
        write_summary(
            call_id,
            state={"source": previous_source, "status": STATUS_IDLE, "updatedAt": now},
        )
        return {"ok": False, "reason": "superseded"}

    write_summary(
        call_id,
        text=summary,
        state={"source": SOURCE_GENERATED, "status": STATUS_IDLE, "updatedAt": now},
    )
    LOGGER.info("Regenerated the summary for %s", call_id)
    return {"ok": True}


def handler(event, context):
    """Entry point for both the AppSync resolver and this function's own invoke."""
    # The AppSync resolver sends the mutation's arguments and the caller's
    # identity; the second leg sends a small message with CallId at the top
    # level.
    arguments = (event.get("arguments") or {}).get("input") or {}
    call_id = arguments.get("CallId") or event.get("CallId")
    if not call_id:
        raise ValueError("no CallId in event")

    # Which meeting and which leg: the two fields of the event this function acts
    # on, named rather than serialised, so the log line says what ran.
    LOGGER.debug("Summary regenerator event for %s, trigger %s", call_id, event.get("Trigger"))

    # Only this function invokes itself, and only without an identity -- an
    # AppSync caller always arrives with one.
    identity = event.get("identity")
    if event.get("Trigger") == "regenerate" and identity is None:
        # A summariser that could not produce a summary is reported, not
        # raised. Only a failure to record the outcome propagates, and it is
        # retried inside write_summary rather than by the platform: the
        # function's EventInvokeConfig sets MaximumRetryAttempts to 0, because
        # re-entering here would pay for another set of model calls for a
        # summary this frame had already computed.
        return run(call_id)

    try:
        return start(call_id, identity, context.invoked_function_arn)
    except Exception:  # noqa: BLE001 - reported to the caller, see below
        LOGGER.exception("Could not start a summary regeneration for %s", call_id)
        return {"Success": False, "Message": "could not start regenerating the summary"}
