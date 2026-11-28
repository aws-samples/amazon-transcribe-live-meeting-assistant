#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Drive a scripted meeting into a deployed LMA stack, or clean one up.

Two delivery paths (see README.md beside this file):

* ``--via kinesis`` (the default) puts START, one ADD_TRANSCRIPT_SEGMENT per turn
  and chat message, and END on the stack's CallDataStream, as a Virtual
  Participant does. Needs only AWS credentials; speaker names are exactly the
  script's.
* ``--via audio`` synthesizes every turn with Amazon Polly, lays the turns out as
  stereo audio (REMOTE speakers on the left channel, LOCAL on the right) and
  streams it to the WebSocket transcriber as a signed-in user, so the meeting
  goes through the real speech path. Chat messages still go through Kinesis at
  their times. Needs a Cognito user, and ``pycognito`` and ``websockets``.

Examples::

    python scripts/synthetic_meetings/run_meeting.py --list
    python scripts/synthetic_meetings/run_meeting.py --stack LMA-dev --meeting sprint_planning --owner jdoe
    python scripts/synthetic_meetings/run_meeting.py --stack LMA-dev --meeting incident_review --speed 4 --no-end
    python scripts/synthetic_meetings/run_meeting.py --stack LMA-dev --meeting retrospective --via audio
    python scripts/synthetic_meetings/run_meeting.py --stack LMA-dev --cleanup "<CallId>"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import delivery  # noqa: E402
import kit  # noqa: E402

DEFAULT_CACHE = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "lma-synthetic-meetings"
)
MEETING_WAIT_SECONDS = 90


def say(message: str) -> None:
    print(message, flush=True)


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Replay a scripted meeting into a deployed LMA stack (see scripts/synthetic_meetings/README.md).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--stack", help="the LMA root stack's name (required except for --list and --dry-run)"
    )
    p.add_argument(
        "--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    p.add_argument(
        "--meeting", help="a bundled meeting's name (see --list) or the path of a script"
    )
    p.add_argument("--list", action="store_true", help="list the bundled meetings and exit")
    p.add_argument("--via", choices=("kinesis", "audio"), default="kinesis")
    p.add_argument(
        "--pace", choices=("live", "fast"), help="default: fast for kinesis, live for audio"
    )
    p.add_argument("--speed", type=float, help="times real time, e.g. 4 (overrides --pace)")
    p.add_argument("--owner", help="the meeting's Owner, a Cognito username (Kinesis path)")
    p.add_argument("--call-id", help="default: '<title> - <UTC time> UTC synthetic-<random>'")
    p.add_argument(
        "--no-end",
        action="store_true",
        help="send no END, so the meeting stays live (Kinesis path)",
    )
    p.add_argument(
        "--stop-after", help="stop part-way: seconds of meeting time (90, 90s) or turns (5turns)"
    )
    p.add_argument(
        "--start-event-extra",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="an extra START field, e.g. GuidanceEnabled=true (repeatable; JSON values are decoded)",
    )
    p.add_argument("--start-event-file", type=Path, help="a JSON object of extra START fields")
    p.add_argument(
        "--cleanup", metavar="CALL_ID", help="delete everything the kit created for this meeting"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="with --cleanup: also for a meeting without the kit's label",
    )
    p.add_argument("--dry-run", action="store_true", help="print what would be sent; send nothing")
    p.add_argument(
        "--no-verify", action="store_true", help="do not read the stored segments back afterwards"
    )
    p.add_argument(
        "--verify-timeout",
        type=float,
        default=None,
        help="seconds to wait for segments (default 60/180)",
    )
    p.add_argument(
        "--summary-json", type=Path, help="also write the closing summary to this file as JSON"
    )
    audio = p.add_argument_group("audio path")
    audio.add_argument(
        "--cognito-username", help="sign in as this user (password: $LMA_TEST_PASSWORD, or asked)"
    )
    audio.add_argument(
        "--cognito-secret-id", help="a Secrets Manager secret holding {username, password}"
    )
    audio.add_argument("--wav-out", type=Path, help="also write the meeting's stereo WAV here")
    audio.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_CACHE, help=f"Polly audio cache ({DEFAULT_CACHE})"
    )
    return p


def _list() -> int:
    for path in kit.bundled_meetings():
        meeting = kit.load_meeting(path)
        seconds = kit.build_timeline(meeting)[-1].end
        say(
            f"{meeting.name:22} {len(meeting.turns):3} turns {len(meeting.chats):2} chat  "
            f"~{seconds / 60:4.1f} min  {meeting.title}"
        )
        if meeting.purpose:
            say(f"{'':22} {meeting.purpose.splitlines()[0]}")
    return 0


def _extra(args: argparse.Namespace) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if args.start_event_file:
        loaded = json.loads(args.start_event_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise SystemExit("--start-event-file must hold a JSON object")
        extra.update(loaded)
    extra.update(kit.parse_extra(args.start_event_extra))
    for key in kit.unknown_start_fields(extra):
        say(
            f"warning: START field {key!r} is not in CreateCallInput; createCall will refuse the meeting"
        )
    return extra


def _cognito_credentials(args: argparse.Namespace, region: str | None) -> tuple[str, str] | None:
    """The audio path's user: the flags, else what integ-tests/cognito_test_user.py reads."""
    if args.cognito_secret_id:
        os.environ["LMA_TEST_USER_SECRET_ID"] = args.cognito_secret_id
    if args.cognito_username:
        password = os.environ.get("LMA_TEST_PASSWORD")
        if not password and sys.stdin.isatty():
            password = getpass.getpass(f"Password for {args.cognito_username}: ")
        if not password:
            raise SystemExit(
                "--cognito-username needs LMA_TEST_PASSWORD set (or a terminal to ask on)"
            )
        return args.cognito_username, password
    sys.path.insert(0, str(ROOT / "integ-tests"))
    import cognito_test_user  # pylint: disable=import-outside-toplevel

    return cognito_test_user.resolve(region)


def _cleanup(args: argparse.Namespace) -> int:
    stack = delivery.Stack.resolve(args.stack, args.region)
    report = delivery.cleanup(
        stack.client("dynamodb"),
        stack.table,
        args.cleanup,
        s3=stack.client("s3") if stack.bucket else None,
        bucket=stack.bucket,
        prefixes=(stack.recordings_prefix, stack.transcripts_prefix, "lma-meeting-archives/"),
        force=args.force,
    )
    if report.refused:
        say(f"not deleted: {report.refused}")
        return 2
    rows = ", ".join(f"{prefix} {count}" for prefix, count in report.rows.items()) or "none"
    say(
        f"Deleted {args.cleanup!r}: rows by partition: {rows}; list rows {report.list_rows}; S3 objects {report.objects}"
    )
    return 0


def _report_segments(
    stack: delivery.Stack, call_id: str, expected: int, timeout: float, settle: float
) -> dict:
    """Wait until the stored rows stop changing (or reach ``expected``) and describe them."""
    ddb = stack.client("dynamodb")
    deadline = time.monotonic() + timeout
    last_count, last_change, rows = -1, time.monotonic(), []
    while time.monotonic() < deadline:
        rows = [r for r in delivery.segments(ddb, stack.table, call_id) if not r.get("IsPartial")]
        if len(rows) != last_count:
            last_count, last_change = len(rows), time.monotonic()
        if expected and len(rows) >= expected:
            break
        if not expected and rows and time.monotonic() - last_change >= settle:
            break
        time.sleep(3)
    by_channel: dict[str, int] = {}
    speakers: dict[str, int] = {}
    for row in rows:
        by_channel[row.get("Channel", "?")] = by_channel.get(row.get("Channel", "?"), 0) + 1
        if row.get("Channel") != kit.CHAT_CHANNEL:
            speakers[str(row.get("Speaker"))] = speakers.get(str(row.get("Speaker")), 0) + 1
    return {
        "stored": len(rows),
        "by_channel": by_channel,
        "speakers": speakers,
        "first_start": float(rows[0]["StartTime"]) if rows else None,
        "last_end": max(float(r.get("EndTime") or 0) for r in rows) if rows else None,
    }


def _run_kinesis(args, stack, meeting, call_id, owner, speed, stop, extra) -> dict:
    sender = delivery.KinesisSender(stack.client("kinesis"), stack.stream, call_id)
    ddb = stack.client("dynamodb")
    timeline = kit.build_timeline(meeting)
    kept, cutoff = kit.truncate(timeline, stop)
    began = time.monotonic()
    sender.put(kit.start_event(meeting, call_id, owner, kit.now_iso(), extra))
    say("START sent; waiting for the meeting record...")
    if not delivery.wait_for(
        lambda: delivery.get_meeting(ddb, stack.table, call_id), MEETING_WAIT_SECONDS
    ):
        raise SystemExit(
            f"no meeting record for {call_id!r} after {MEETING_WAIT_SECONDS}s; the CallEventProcessor's log says why"
        )
    say(f"Meeting created ({time.monotonic() - began:.1f}s). Sending {len(kept)} events...")
    origin = time.monotonic()
    if speed is None:
        now = kit.now_iso()
        sender.put_many([kit.segment_event(call_id, item, now) for item in kept])
    else:
        for item in kept:
            delay = origin + kit.wall_offset(item.send_at, speed) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            sender.put(kit.segment_event(call_id, item, kit.now_iso()))
            mark = "chat" if item.kind == "chat" else item.channel
            say(f"  [{_clock(item.start)}] {mark:6} {item.name}: {item.text[:70]}")
    if not args.no_end:
        sender.put(kit.end_event(call_id, kit.now_iso()))
    return {
        "sent_segments": sum(1 for t in kept if t.turn),
        "sent_chats": sum(1 for t in kept if not t.turn),
        "ended": not args.no_end,
        "meeting_seconds": round(cutoff, 1),
        "wall_seconds": round(time.monotonic() - began, 1),
    }


def _run_audio(args, stack, meeting, call_id, speed, stop, tokens) -> dict:
    import websockets  # pylint: disable=import-outside-toplevel

    pcm = delivery.synthesize_meeting(stack.client("polly"), meeting, args.cache_dir, say)
    durations = {index: kit.pcm_seconds(data) for index, data in pcm.items()}
    timeline = kit.build_timeline(meeting, durations)
    kept, cutoff = kit.truncate(timeline, stop)
    stereo = kit.assemble_stereo(timeline, pcm, end_at=cutoff + 0.5 if stop else None)
    if args.wav_out:
        args.wav_out.write_bytes(kit.wav_bytes(stereo))
        say(f"Wrote {args.wav_out}")
    chats = [t for t in kept if not t.turn]
    sender = delivery.KinesisSender(stack.client("kinesis"), stack.stream, call_id)
    turns = iter([t for t in kept if t.turn])
    next_turn = next(turns, None)

    def on_time(meeting_time: float) -> None:
        nonlocal next_turn
        while chats and chats[0].send_at <= meeting_time:
            item = chats.pop(0)
            sender.put(kit.segment_event(call_id, item, kit.now_iso()))
            say(f"  [{_clock(item.start)}] chat   {item.name}: {item.text[:70]}")
        while next_turn is not None and next_turn.start <= meeting_time:
            say(
                f"  [{_clock(next_turn.start)}] {next_turn.channel:6} {next_turn.name}: {next_turn.text[:70]}"
            )
            next_turn = next(turns, None)

    frame = kit.websocket_start_frame(meeting, call_id)
    seconds = kit.pcm_seconds(stereo) / 2
    say(f"Streaming {seconds:.0f}s of audio at {speed:g}x (about {seconds / speed:.0f}s)...")
    began = time.monotonic()

    def connect(url: str):
        return websockets.connect(url, max_size=None, open_timeout=25)

    state = asyncio.run(
        delivery.stream_meeting(
            connect,
            delivery.websocket_url(stack.outputs["LMAWebsocketEndpoint"], tokens),
            frame,
            list(kit.audio_chunks(stereo)),
            speed,
            on_time,
        )
    )
    on_time(cutoff)
    return {
        "sent_segments": sum(1 for t in kept if t.turn),
        "sent_chats": sum(1 for t in kept if not t.turn),
        "ended": True,
        "meeting_seconds": round(seconds, 1),
        "wall_seconds": round(time.monotonic() - began, 1),
        "socket_state": state,
        "diarized": {"REMOTE": frame["diarizeSystemChannel"], "LOCAL": frame["diarizeMicChannel"]},
    }


def _dry_run(args, meeting, call_id, owner, speed, stop, extra) -> int:
    timeline = kit.build_timeline(meeting)
    kept, cutoff = kit.truncate(timeline, stop)
    say(f"# dry run: {len(kept)} events over {cutoff:.1f}s of meeting time; nothing is sent")
    if args.via == "audio":
        say(json.dumps({"websocket_start": kit.websocket_start_frame(meeting, call_id)}))
        say("# turn times below are estimates; the audio path uses the synthesized lengths")
    else:
        say(
            json.dumps(
                {
                    "at": 0.0,
                    "record": kit.start_event(meeting, call_id, owner, kit.now_iso(), extra),
                }
            )
        )
    now = kit.now_iso()
    for item in kept:
        if args.via == "audio" and item.turn:
            say(
                json.dumps(
                    {
                        "at": round(kit.wall_offset(item.start, speed), 2),
                        "audio_turn": item.__dict__,
                    }
                )
            )
        else:
            record = kit.segment_event(call_id, item, now)
            say(
                json.dumps({"at": round(kit.wall_offset(item.send_at, speed), 2), "record": record})
            )
    if args.via == "audio" or not args.no_end:
        say(
            json.dumps(
                {
                    "at": round(kit.wall_offset(cutoff, speed), 2),
                    "record": kit.end_event(call_id, now),
                }
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        return _list()
    if args.cleanup:
        if not args.stack:
            raise SystemExit("--cleanup needs --stack")
        return _cleanup(args)
    if not args.meeting:
        raise SystemExit("--meeting is required (see --list)")
    meeting = kit.find_meeting(args.meeting)
    for warning in kit.warnings(meeting):
        say(f"warning: {warning}")
    try:
        speed = kit.resolve_speed(args.via, args.pace, args.speed)
        stop = kit.parse_stop_after(args.stop_after) if args.stop_after else None
    except ValueError as err:
        raise SystemExit(str(err)) from err
    extra = _extra(args)
    if args.via == "audio":
        if args.no_end:
            raise SystemExit(
                "--no-end works on the Kinesis path only: the transcriber ends the meeting when the stream closes"
            )
        if extra:
            say(
                "warning: --start-event-extra applies to the Kinesis START only; the audio path ignores it"
            )
        if speed > kit.AUDIO_FAST_SPEED:
            say(
                f"warning: audio faster than {kit.AUDIO_FAST_SPEED:g}x real time has not been measured"
            )
    call_id = args.call_id or kit.default_call_id(
        meeting, datetime.now(timezone.utc), uuid.uuid4().hex[:4]
    )

    if args.dry_run:
        return _dry_run(args, meeting, call_id, args.owner or "<owner>", speed, stop, extra)
    if not args.stack:
        raise SystemExit("--stack is required")

    stack = delivery.Stack.resolve(args.stack, args.region)
    tokens = None
    if args.via == "audio":
        credentials = _cognito_credentials(args, stack.region)
        if credentials is None:
            raise SystemExit(
                "--via audio needs a Cognito user: --cognito-username, --cognito-secret-id, "
                "or LMA_TEST_USERNAME/LMA_TEST_PASSWORD or LMA_TEST_USER_SECRET_ID"
            )
        outputs = stack.outputs
        tokens = delivery.mint_tokens(
            stack.region,
            delivery.user_pool_id(outputs),
            outputs["CognitoUserPoolClientId"],
            *credentials,
        )
        owner = None  # the signed-in user; read back from the meeting record below
    else:
        # A sign-in email is resolved to the pool's username, which is what the web
        # UI matches a meeting's Owner against; a name not in the pool is kept as given.
        name = args.owner
        if not name:
            credentials = _cognito_credentials(args, stack.region)
            if credentials is None:
                raise SystemExit(
                    "--owner is required (no Cognito test user is configured to default to)"
                )
            name = credentials[0]
        owner = delivery.resolve_owner(
            stack.client("cognito-idp"), delivery.user_pool_id(stack.outputs), name
        )

    say(f"Meeting:  {meeting.name} ({meeting.title})")
    say(f"CallId:   {call_id}")
    say(
        f"Page:     {delivery.meeting_url(stack.outputs, call_id) or '(no ApplicationCloudfrontEndpoint output)'}"
    )
    say(f"Delivery: {args.via}, {'no waiting' if speed is None else f'{speed:g}x real time'}")

    if args.via == "audio":
        result = _run_audio(args, stack, meeting, call_id, speed, stop, tokens)
    else:
        result = _run_kinesis(args, stack, meeting, call_id, owner, speed, stop, extra)
    summary: dict[str, Any] = {
        "call_id": call_id,
        "meeting": meeting.name,
        "via": args.via,
        **result,
    }
    # Reported from the meeting record, which says whom the stack made the Owner.
    row = delivery.get_meeting(stack.client("dynamodb"), stack.table, call_id) or {}
    summary["owner"] = row.get("Owner")
    say(f"Owner:    {summary['owner'] or '(no meeting record yet)'}")
    if not args.no_verify:
        expected = result["sent_segments"] + result["sent_chats"] if args.via == "kinesis" else 0
        timeout = args.verify_timeout or (60 if args.via == "kinesis" else 180)
        say("Reading the stored segments back...")
        summary["stored"] = _report_segments(stack, call_id, expected, timeout, settle=20)
    say("")
    say(
        f"Sent {result['sent_segments']} turns and {result['sent_chats']} chat messages in {result['wall_seconds']}s"
    )
    if "stored" in summary:
        stored = summary["stored"]
        say(f"Stored {stored['stored']} final rows: {stored['by_channel']}")
        say(f"Speakers seen: {stored['speakers']}")
    say(f"Meeting {'ended' if result['ended'] else 'left live (no END sent)'}: {call_id}")
    say(
        f"Clean up:  python scripts/synthetic_meetings/run_meeting.py --stack {args.stack} "
        f"--cleanup {json.dumps(call_id)}{' --force' if args.via == 'audio' else ''}"
    )
    if args.summary_json:
        args.summary_json.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
