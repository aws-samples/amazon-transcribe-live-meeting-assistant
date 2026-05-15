# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Kinesis injector — the zero-cost, highest-throughput synthetic meeting
driver.

Produces START → ADD_TRANSCRIPT_SEGMENT* → END (+ optional ADD_SUMMARY)
events onto LMA's CallDataStream Kinesis stream with the exact shape
``call_event_processor`` expects from the live websocket transcriber and
the upload_meeting_finalizer. This lets us:

1. **Back-fill historical meetings** — any ``CreatedAt`` is accepted; LMA's
   DynamoDB meeting-list GSIs ``listCallsDateRange`` and ``getCallCount`` bucket
   by that timestamp. We can fabricate tens of thousands of backdated
   meetings in minutes, perfect for UI list/date-picker scale testing.

2. **Drive live-style concurrent tests** without a Transcribe bill —
   scenarios that don't care about audio fidelity can emit this synthetic
   traffic to smoke-test the event-processor / DDB / AppSync subscription
   fan-out path.

### Ordering guarantees — why we emit in phases

Early versions queued [START, seg1..segN, END] for a single meeting into the
**same** Kinesis PutRecords batch and relied on matching PartitionKey to
preserve order. That's insufficient: the downstream Lambda receives an array
of records and processes them with ``asyncio.gather`` — in-batch ordering is
lost, and END can race ahead of createCall or of the last segments, producing
meetings stuck in "In Progress" or ENDED rows with no transcript (→ Bedrock
summary then errors out).

The :class:`~lma_load.scenarios.backfill.BackfillScenario` (and anything else
that builds N meetings at once) now calls the driver in **three phases**::

    phase 1:  emit all STARTs   → flush → sleep
    phase 2:  emit all segments → flush → sleep
    phase 3:  emit all ENDs (+ ADD_SUMMARY if requested) → flush

Each phase uses its own Kinesis PutRecords call(s), so the processor Lambda
sees START events fully resolved in DDB before any segment lands, and so on.
The inter-phase sleeps are tuned to cover the Lambda's event-source-mapping
polling window (~1–2 s) so the next phase's events land on a fresh
invocation.

### Event shapes we produce (ref: event_processor/call_event_processor.py)

START  (becomes createCall → DDB row with CreatedAt):
    {
      "EventType": "START",
      "CallId": "<str>",
      "CustomerPhoneNumber": "<str>",
      "SystemPhoneNumber": "<str>",
      "AgentId": "<owner>",
      "CreatedAt": "<iso8601>"
    }

ADD_TRANSCRIPT_SEGMENT (becomes addTranscriptSegment → DDB row):
    {
      "EventType": "ADD_TRANSCRIPT_SEGMENT",
      "CallId": "<str>",
      "Channel": "AGENT" | "CALLER",
      "SegmentId": "<uuid>",
      "StartTime": float,       # seconds offset
      "EndTime":   float,
      "Transcript": "<text>",
      "IsPartial": false,
      "Speaker":   "<label>",
      "CreatedAt": "<iso8601>",  # back-dated per-segment
      "Status":    "TRANSCRIBING"
    }

END  (becomes updateCallStatus → ENDED + summary orchestrator trigger):
    {
      "EventType": "END",
      "CallId": "<str>",
      "UpdatedAt": "<iso8601>",
      "CreatedAt": "<iso8601>"
    }

ADD_SUMMARY  (becomes addCallSummaryText → Call.CallSummaryText; overrides
anything the real Bedrock orchestrator may subsequently write):
    {
      "EventType": "ADD_SUMMARY",
      "CallId": "<str>",
      "CallSummaryText": "<markdown>",
      "UpdatedAt": "<iso8601>",
      "CreatedAt": "<iso8601>"
    }

All records for a given callId share the same PartitionKey (== callId) so
that whatever Lambda invocation does receive them sees them in the order
they were emitted.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Synthetic transcript corpus — 3 personas × ~40 lines each.
# ---------------------------------------------------------------------------
# We ship enough variety that a 45-minute synthetic meeting (segment_count=18)
# still reads naturally and gives Bedrock enough content to summarise when
# --with-summary is used. Each line is (channel, speaker_persona, text) and
# the injector picks a persona per-meeting, then cycles through its lines.
#
# Keep everything benign and corporate — these land in real customer stacks
# during load-test runs.

_PERSONA_SALES_CALL: list[tuple[str, str]] = [
    ("AGENT",  "Thanks for joining today. I wanted to walk through the quarterly pipeline review."),
    ("CALLER", "Great, happy to be here. Can you share your screen?"),
    ("AGENT",  "Sure — I'll pull up the dashboard now. You should be seeing it."),
    ("CALLER", "Yes, coming through clearly. These numbers look strong, especially in mid-market."),
    ("AGENT",  "Yes, we saw a 22% uptick versus last quarter, driven mostly by expansion deals."),
    ("CALLER", "What about churn in the SMB cohort? That was the concern last time we spoke."),
    ("AGENT",  "Churn ticked down two points thanks to the new onboarding flow we rolled out."),
    ("CALLER", "Do we have case studies I can share with my team for the internal review?"),
    ("AGENT",  "I'll send three customer references over after this call — all in your segment."),
    ("CALLER", "Perfect. Any asks on our side to keep the momentum going?"),
    ("AGENT",  "Yes — could we schedule a technical review with your architecture team next week?"),
    ("CALLER", "Absolutely, I'll ping Priya and get it on the calendar this week."),
    ("AGENT",  "Appreciated. Let's also finalise the contract renewal by month-end if we can."),
    ("CALLER", "Agreed. I'll chase the legal team today — there was one redline remaining."),
    ("AGENT",  "Happy to jump on a quick call with legal if that unblocks things."),
    ("CALLER", "That would help. Tuesday afternoon tends to work for them."),
    ("AGENT",  "Booked — I'll send the invite as soon as we're done here."),
    ("CALLER", "Great. What about the enterprise tier — any updates on the timeline?"),
    ("AGENT",  "Beta cohort goes live week of the 15th. You're on the list."),
    ("CALLER", "Excellent. We're especially interested in the improved SSO story."),
    ("AGENT",  "SSO is one of the flagship features. Full SAML and OIDC plus just-in-time provisioning."),
    ("CALLER", "That'll make security review a lot easier on our end."),
    ("AGENT",  "That's the plan. Anything else you'd like to cover today?"),
    ("CALLER", "Not from my side. Great catch-up as always."),
    ("AGENT",  "Same here — I'll follow up with the references and the legal invite. Talk soon."),
]

_PERSONA_STANDUP: list[tuple[str, str]] = [
    ("AGENT",  "Morning everyone, let's kick off the standup. I'll go first today."),
    ("CALLER", "Good morning. Sounds good."),
    ("AGENT",  "Yesterday I wrapped up the DynamoDB index migration for the meeting-list query."),
    ("CALLER", "Nice — did the backfill job finish cleanly?"),
    ("AGENT",  "It did, eventually. There were a couple of throttling blips but nothing that needed intervention."),
    ("CALLER", "Good to hear. Any follow-up work?"),
    ("AGENT",  "I still need to update the dashboard to point at the new GSI, but that's a ten-minute change."),
    ("CALLER", "My turn. Yesterday I got the vp-loader embed demo working end-to-end."),
    ("AGENT",  "That's the one with autoStart from the URL?"),
    ("CALLER", "Exactly. The postMessage handshake was trickier than expected but it's solid now."),
    ("AGENT",  "Sweet. Are you blocked on anything?"),
    ("CALLER", "Not currently. I'm going to start on the Puppeteer test-harness today so we can load-test at scale."),
    ("AGENT",  "Love it. That's going to pair well with the load simulator work."),
    ("CALLER", "Speaking of — any progress on the stack-info resolver changes?"),
    ("AGENT",  "Yes, I shipped it yesterday. It now parses LocalUITestingEnv as a fallback, which handles every stack version we've seen."),
    ("CALLER", "Nice fallback strategy. What's next on your plate?"),
    ("AGENT",  "I want to tackle the Bedrock quota probe — right now it just prints a warning if the estimate exceeds the limit."),
    ("CALLER", "That would be useful. Could we also capture the actual Bedrock RPM during a run?"),
    ("AGENT",  "Good idea. I'll see if CloudWatch exposes that at a granular enough resolution."),
    ("CALLER", "One other thing — the deployment pipeline is flaky again this week."),
    ("AGENT",  "I saw. I think it's the CodeBuild capacity issue we flagged last sprint."),
    ("CALLER", "Want me to open a ticket with the platform team?"),
    ("AGENT",  "Please. That should unblock the CI retries too."),
    ("CALLER", "On it. Any other blockers?"),
    ("AGENT",  "Nope, we're good. Let's sync again tomorrow same time."),
]

_PERSONA_DESIGN_REVIEW: list[tuple[str, str]] = [
    ("AGENT",  "Thanks for joining the design review. I'll give a quick overview then open it up."),
    ("CALLER", "Sounds good. How much time do we have?"),
    ("AGENT",  "Thirty minutes, tops. I've pre-circulated the doc so we'll skip the background section."),
    ("CALLER", "Great — I had a chance to read through most of it."),
    ("AGENT",  "The core change is moving the transcript storage from per-call S3 objects to a consolidated Glue-catalogued Iceberg table."),
    ("CALLER", "That's a big shift. What's the motivating use case?"),
    ("AGENT",  "Two things. Analytics queries across meetings, and cheaper cold storage via automatic Iceberg compaction."),
    ("CALLER", "Makes sense. What's the impact on the live read path?"),
    ("AGENT",  "Minimal. We keep a hot copy in DynamoDB for active meetings; Iceberg is strictly the cold archive."),
    ("CALLER", "OK. What about the migration? We have thousands of meetings in the old format."),
    ("AGENT",  "There's a batch job that walks the existing S3 prefix and writes to the new table. Runs nightly."),
    ("CALLER", "Can it run backfills for historical data too?"),
    ("AGENT",  "Yes — the same job handles both. It's idempotent, so re-runs are safe."),
    ("CALLER", "How do we handle schema evolution? Iceberg supports it, but the consumers might not."),
    ("AGENT",  "We'll version the schema in Glue. Consumers pin to a major version."),
    ("CALLER", "Good. Any concerns from the security side?"),
    ("AGENT",  "We're reusing the existing KMS CMK, and LakeFormation enforces row-level access via the existing Owner attribute."),
    ("CALLER", "That's cleaner than the current S3 bucket-policy approach."),
    ("AGENT",  "Agreed. What about observability?"),
    ("CALLER", "We get Iceberg metrics for free through the Glue catalog. I'll wire them into the main CloudWatch dashboard."),
    ("AGENT",  "Perfect. Any objections to moving forward?"),
    ("CALLER", "Not from me. The design addresses the concerns I raised last round."),
    ("AGENT",  "Great. I'll file the RFC and we can sign off async."),
    ("CALLER", "Sounds good. I'll review it by end of week."),
    ("AGENT",  "Thanks everyone. Talk later."),
]

_PERSONAS: list[tuple[str, list[tuple[str, str]]]] = [
    ("sales",     _PERSONA_SALES_CALL),
    ("standup",   _PERSONA_STANDUP),
    ("design",    _PERSONA_DESIGN_REVIEW),
]


def iso(t: datetime) -> str:
    """Milli-second ISO-8601 with UTC offset, the shape LMA's VTLs use."""
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _pick_persona(call_id: str) -> list[tuple[str, str]]:
    """Deterministic persona choice so re-runs with the same run-id produce
    identical transcripts (useful for testing & cleanup correlation)."""
    h = sum(ord(c) for c in call_id)
    return _PERSONAS[h % len(_PERSONAS)][1]


# ---------------------------------------------------------------------------
# Synthetic summary template
# ---------------------------------------------------------------------------
_SYNTHETIC_SUMMARY_TEMPLATE = (
    "# Synthetic Meeting Summary\n"
    "\n"
    "_This meeting was fabricated by the LMA Load Simulator "
    "(runId `{run_id}`) and does not represent real content._\n"
    "\n"
    "## Overview\n"
    "- Meeting style: **{persona}**\n"
    "- Duration: {duration_min:.0f} min\n"
    "- Segments: {segments}\n"
    "\n"
    "## Key Topics\n"
    "- Quarterly pipeline + expansion metrics\n"
    "- Churn trend in SMB segment, post-onboarding rollout\n"
    "- Architecture review scheduling\n"
    "- Contract renewal timeline\n"
    "\n"
    "## Action Items\n"
    "- Share three customer references\n"
    "- Schedule architecture deep-dive\n"
    "- Chase legal redline\n"
    "\n"
    "> Generated synthetically by lma-load-simulator; "
    "`--with-summary` would have triggered the real Bedrock summary instead."
)


@dataclass
class SyntheticMeetingSpec:
    """Parameters for one fabricated meeting."""

    call_id: str
    owner: str                   # AgentId → becomes Owner via VP-style fallback
    created_at: datetime         # meeting start timestamp (can be historical)
    duration_s: float = 300.0    # total length in seconds
    segment_count: int = 10
    from_number: str = "Customer"
    to_number: str = "System"

    # If set, an ADD_SUMMARY event will be emitted during the END phase with
    # this text, overriding whatever the real Bedrock orchestrator produces.
    # The backfill scenario populates this when --skip-summary is used; it's
    # left as None when --with-summary is in effect so Bedrock runs normally.
    summary_text: str | None = None


def make_synthetic_summary(spec: SyntheticMeetingSpec, run_id: str) -> str:
    """Build a canned summary that guarantees the Summary column is populated
    even when we've explicitly skipped Bedrock."""
    persona_name = _PERSONAS[sum(ord(c) for c in spec.call_id) % len(_PERSONAS)][0]
    return _SYNTHETIC_SUMMARY_TEMPLATE.format(
        run_id=run_id,
        persona=persona_name,
        duration_min=spec.duration_s / 60.0,
        segments=spec.segment_count,
    )


# ---------------------------------------------------------------------------
# Injector — low-level Kinesis batcher
# ---------------------------------------------------------------------------
class KinesisInjector:
    """Thread-safe Kinesis PutRecords batcher.

    Two ways to use it:

    * :meth:`emit_meeting` — append **all** events for one meeting
      (START → segments → END[+summary]) to the buffer. Convenient but
      means the buffer may contain events from many meetings interleaved
      before a flush, and a single Lambda invocation may process segments
      for a callId whose START is still in flight. Good for low-volume
      one-shot tests; **not** safe for the high-volume backfill path.

    * :meth:`emit_phase` — caller specifies a single phase ("START",
      "SEGMENTS", "END") and passes the specs. The injector buffers
      just those events, flushes, and the caller sleeps between phases.
      This is how :mod:`lma_load.scenarios.backfill` uses the injector to
      guarantee end-to-end correctness.
    """

    # Kinesis PutRecords caps: 500 records or 5 MiB per call.
    MAX_RECORDS_PER_BATCH = 500
    MAX_BYTES_PER_BATCH = 4 * 1024 * 1024  # leave headroom

    def __init__(
        self,
        stream_name: str,
        region: str,
        profile: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.stream_name = stream_name
        self.region = region
        if client is not None:
            self.kinesis = client
        else:
            session_kwargs: dict[str, Any] = {"region_name": region}
            if profile:
                session_kwargs["profile_name"] = profile
            self.kinesis = boto3.Session(**session_kwargs).client(
                "kinesis",
                config=Config(retries={"mode": "adaptive", "max_attempts": 6}),
            )
        self._buf: list[dict] = []
        self._buf_bytes = 0
        self._emitted = 0
        self._failed = 0

    # ── Whole-meeting emission (use with care — see class docstring) ──

    def emit_meeting(self, spec: SyntheticMeetingSpec) -> int:
        """Queue START + all segments + END[+summary] for one meeting."""
        events = list(_build_events(spec))
        for ev in events:
            self._buffer(spec.call_id, ev)
        return len(events)

    def emit_many(self, specs: Iterable[SyntheticMeetingSpec]) -> int:
        """Queue all events for many meetings in one interleaved batch."""
        total = 0
        for s in specs:
            total += self.emit_meeting(s)
        self.flush()
        return total

    # ── Phased emission (the correct path for backfill / high volume) ──

    PHASES = ("START", "SEGMENTS", "END")

    def emit_phase(
        self,
        phase: str,
        specs: Iterable[SyntheticMeetingSpec],
        run_id: str | None = None,
    ) -> int:
        """Emit the events for a single phase across many meetings.

        ``run_id`` is only required when ``phase == "END"`` and any spec
        carries ``summary_text``/opt-out metadata — it's embedded in the
        synthetic ADD_SUMMARY event for traceability.
        """
        if phase not in self.PHASES:
            raise ValueError(f"Unknown phase {phase!r}; expected one of {self.PHASES}")
        count = 0
        for spec in specs:
            for ev in _phase_events(phase, spec, run_id=run_id):
                self._buffer(spec.call_id, ev)
                count += 1
        self.flush()
        return count

    # ── Buffering / batching ──────────────────────────────────

    def _buffer(self, partition_key: str, event: dict) -> None:
        data = json.dumps(event, separators=(",", ":")).encode("utf-8")
        if (
            len(self._buf) >= self.MAX_RECORDS_PER_BATCH
            or self._buf_bytes + len(data) > self.MAX_BYTES_PER_BATCH
        ):
            self._flush_batch()
        self._buf.append({"Data": data, "PartitionKey": partition_key})
        self._buf_bytes += len(data)

    def flush(self) -> None:
        """Drain any pending buffer."""
        while self._buf:
            self._flush_batch()

    def _flush_batch(self) -> None:
        if not self._buf:
            return
        records = self._buf
        self._buf = []
        self._buf_bytes = 0
        try:
            resp = self.kinesis.put_records(StreamName=self.stream_name, Records=records)
        except ClientError as err:
            logger.error("Kinesis put_records failed: %s", err)
            self._failed += len(records)
            return
        failed = int(resp.get("FailedRecordCount", 0))
        self._emitted += len(records) - failed
        self._failed += failed
        if failed:
            # Re-queue just the records that failed (typically a throttle on
            # a hot shard) for a retry on the next flush.
            for i, r in enumerate(resp.get("Records", [])):
                if "ErrorCode" in r:
                    self._buf.append(records[i])
                    self._buf_bytes += len(records[i]["Data"])
            time.sleep(0.25)  # back-off before caller retries

    # ── Stats ─────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, int]:
        return {"emitted": self._emitted, "failed": self._failed}


# ---------------------------------------------------------------------------
# Event fabrication
# ---------------------------------------------------------------------------
def _build_events(spec: SyntheticMeetingSpec) -> Iterable[dict]:
    """Yield START, N segments, END (+ summary) for a single meeting.

    Used by :meth:`KinesisInjector.emit_meeting` when the caller wants
    a one-shot all-events-queued approach. The phase-based emission path
    calls :func:`_phase_events` instead.
    """
    yield from _phase_events("START", spec)
    yield from _phase_events("SEGMENTS", spec)
    yield from _phase_events("END", spec)


def _phase_events(
    phase: str, spec: SyntheticMeetingSpec, run_id: str | None = None
) -> Iterable[dict]:
    """Yield only the events belonging to ``phase``."""
    if phase == "START":
        # call_event_processor.execute_create_call_mutation uses AgentId as
        # the Owner when no JWT is present — matches the VP service path.
        yield {
            "EventType": "START",
            "CallId": spec.call_id,
            "CustomerPhoneNumber": spec.from_number,
            "SystemPhoneNumber": spec.to_number,
            "AgentId": spec.owner,
            "CreatedAt": iso(spec.created_at),
        }
        return

    if phase == "SEGMENTS":
        if spec.segment_count <= 0:
            return
        persona = _pick_persona(spec.call_id)
        step = spec.duration_s / spec.segment_count
        for i in range(spec.segment_count):
            start_s = i * step
            end_s = min(spec.duration_s, (i + 1) * step - 0.1)
            # IMPORTANT: normalize_transcript_segments in the event-processor
            # layer uses `if message.get("StartTime", None):` which is FALSY
            # when StartTime is exactly 0.0, causing the non-null Float to be
            # dropped and the GraphQL mutation to reject the record. Guarantee
            # a strictly positive value to avoid that truthiness bug.
            start_s_safe = max(round(start_s, 3), 0.001)
            end_s_safe = max(round(end_s, 3), start_s_safe + 0.001)
            channel, text = persona[i % len(persona)]
            seg_created_at = spec.created_at + timedelta(seconds=start_s)
            yield {
                "EventType": "ADD_TRANSCRIPT_SEGMENT",
                "CallId": spec.call_id,
                "Channel": channel,
                "SegmentId": str(uuid.uuid4()),
                "StartTime": start_s_safe,
                "EndTime": end_s_safe,
                "Transcript": text,
                "IsPartial": False,
                "Speaker": "Alice" if channel == "AGENT" else "Bob",
                "CreatedAt": iso(seg_created_at),
                "Status": "TRANSCRIBING",
                "OriginalTranscript": text,
            }
        return


    if phase == "END":
        # IMPORTANT: updateCall.request.vtl guards every mutation with the
        # condition ``#UpdatedAt < :UpdatedAt``. The new UpdatedAt must be
        # strictly greater than whatever createCall wrote (which is always
        # "now" for the record being updated, even when CreatedAt is
        # backdated), *and* greater than any previous update that landed in
        # the same Lambda-batch ``asyncio.gather`` call. If they collide,
        # the later-arriving mutation silently fails and the row is left in
        # STARTED forever.
        #
        # Strategy: emit ADD_SUMMARY first (smaller UpdatedAt), then END
        # (larger UpdatedAt). That way, whichever order the two mutations
        # end up executing in inside the batch processor, the END's
        # UpdatedAt always wins and the row reaches ENDED.
        #
        # This doesn't affect the UI's Duration column (which uses
        # TotalConversationDurationMillis, not UpdatedAt - CreatedAt) and
        # matches reality — the DDB row is genuinely being updated *now*.
        now = datetime.now(timezone.utc)
        if spec.summary_text:
            yield {
                "EventType": "ADD_SUMMARY",
                "CallId": spec.call_id,
                "CallSummaryText": spec.summary_text,
                "CreatedAt": iso(spec.created_at),
                "UpdatedAt": iso(now),
            }
        # END must come after ADD_SUMMARY so its UpdatedAt strictly wins
        # the VTL ``<`` guard regardless of intra-batch processing order.
        yield {
            "EventType": "END",
            "CallId": spec.call_id,
            "CreatedAt": iso(spec.created_at),
            "UpdatedAt": iso(now + timedelta(milliseconds=500)),
        }
        return




# ---------------------------------------------------------------------------
# Convenience factory for backfill scenarios
# ---------------------------------------------------------------------------
def make_backfill_specs(
    count: int,
    run_id: str,
    days_back: int,
    owners: list[str],
    duration_min_range: tuple[float, float] = (10, 45),
    segment_count_range: tuple[int, int] = (6, 18),
    rng: random.Random | None = None,
    include_synthetic_summary: bool = False,
) -> list[SyntheticMeetingSpec]:
    """Build ``count`` SyntheticMeetingSpec objects spread uniformly across
    the last ``days_back`` days, round-robin-owned across ``owners``.

    When ``include_synthetic_summary`` is True, each spec is pre-populated
    with a canned summary so the backfill scenario emits ADD_SUMMARY events
    that overwrite whatever Bedrock writes. Use this with ``--skip-summary``.
    """
    rng = rng or random.Random()
    now = datetime.now(timezone.utc)
    specs: list[SyntheticMeetingSpec] = []
    for i in range(count):
        seconds_back = rng.random() * days_back * 24 * 3600
        created = now - timedelta(seconds=seconds_back)
        owner = owners[i % len(owners)] if owners else f"loadtest-{run_id}-u0001"
        duration_s = 60.0 * rng.uniform(*duration_min_range)
        segs = rng.randint(*segment_count_range)
        call_id = _stable_call_id(run_id, i, created)
        spec = SyntheticMeetingSpec(
            call_id=call_id,
            owner=owner,
            created_at=created,
            duration_s=duration_s,
            segment_count=segs,
        )
        if include_synthetic_summary:
            spec.summary_text = make_synthetic_summary(spec, run_id)
        specs.append(spec)
    specs.sort(key=lambda s: s.created_at)
    return specs


def _stable_call_id(run_id: str, idx: int, created_at: datetime) -> str:
    """Build a human-readable callId for backfilled meetings.

    Format: ``loadtest meeting-<idx> run-id=<run_id>``

    We drop the ISO timestamp that used to trail the callId — the Meeting
    Detail page already shows CreatedAt in its own column, so the stamp was
    redundant and made the ID long and hard to scan. The ``run-id=`` label
    makes the run-id easy to copy/paste for use with
    ``lma load cleanup --target-run-id <id>``.

    The ``created_at`` argument is retained for API stability (cleanup code
    and tests reference the signature) but is no longer part of the output.
    """
    del created_at  # intentionally unused; see docstring.
    return f"loadtest meeting-{idx:06d} run-id={run_id}"
