#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A scripted guest in a real Google Meet meeting, run from an LMA stack.

The guest is a one-off ECS task started from the stack's own Virtual
Participant image (``guest.mjs`` beside this file). It joins the meeting under
a name of your choosing, plays a synthetic conversation into its microphone,
and then follows commands: send a chat message, present a tab of slides, take a
screenshot, leave. It is how demo recordings get a second participant, and how
the scheduled end-to-end test (``integ-tests/test_meet_end_to_end.py``) gets
speech and chat into a meeting.

Containment: the guest's task definition has **no task role**, so the browser in
it holds no AWS credentials. Its audio, its commands and its screenshot move
through presigned S3 URLs, scoped to one prefix of the stack's recordings
bucket, which this tool deletes when the guest is stopped.

Command line (state is kept in ``--state``, default ``.meeting-guest.json``)::

    meeting_guest.py start --stack LMA --meet-url https://meet.google.com/abc-defg-hij
    meeting_guest.py wait          # until the guest is in the call (or gave up)
    meeting_guest.py chat "LMA what was just discussed?"
    meeting_guest.py present
    meeting_guest.py shot screenshot.png
    meeting_guest.py leave         # leave, stop the task, delete its S3 objects

Uses ``AWS_PROFILE`` / ``AWS_REGION`` like every other repo command, and Amazon
Polly to synthesize the conversation (or ``--audio`` for a WAV of your own).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import boto3
from botocore.config import Config

HERE = Path(__file__).resolve().parent
GUEST_SCRIPT = HERE / "guest.mjs"
CONTAINER_NAME = "guest"
LOG_STREAM_PREFIX = "meeting-guest"
S3_PREFIX = "meeting-guest"
URL_TTL_SECONDS = 4 * 3600
IN_CALL = "IN CALL"
GAVE_UP = "GAVE UP"

# A short planning meeting between two voices. Speech for the transcript, and
# introductions the speaker identification can name people from.
CONVERSATION = [
    ("Ruth", "Hi everyone, I'm Priya Shah, I lead product for the mobile app. Thanks for joining."),
    ("Stephen", "Hi Priya, I'm Marcus Lee from engineering. Happy to be here."),
    (
        "Ruth",
        "We have three things on the agenda today: the March launch date, the beta feedback, "
        "and hiring for the support team.",
    ),
    ("Ruth", "Let's start with the launch. Marcus, are we still on track for March the twentieth?"),
    (
        "Stephen",
        "Mostly. The payments screen is done, but offline sync still needs about two weeks of work, "
        "and we haven't decided whether it blocks the launch or ships in a later update.",
    ),
    (
        "Ruth",
        "Okay. That decision needs to happen this week. Someone should write up the trade-offs.",
    ),
    ("Stephen", "Agreed. And we should submit the build to the app store by March the tenth."),
    ("Ruth", "Great. Thanks Marcus, that covers everything for today."),
]


@dataclass
class VpResources:
    """What a guest task needs from the stack's Virtual Participant nested stack."""

    cluster_arn: str
    task_definition_arn: str
    subnets: list[str]
    security_groups: list[str]
    bucket: str


@dataclass
class GuestState:
    """Everything needed to talk to a running guest, saved between CLI calls."""

    stack: str
    region: str
    cluster_arn: str
    task_arn: str
    bucket: str
    prefix: str
    log_group: str
    commands: list[str] = field(default_factory=list)

    @property
    def task_id(self) -> str:
        return self.task_arn.rsplit("/", 1)[-1]

    @property
    def log_stream(self) -> str:
        return f"{LOG_STREAM_PREFIX}/{CONTAINER_NAME}/{self.task_id}"


def container_command(script: str) -> str:
    """The shell command the guest container runs: a display, then guest.mjs.

    The script travels inside the task definition (base64, so no quoting can
    break it) rather than as a download, which keeps the run-time overrides
    small: ECS caps them at 8 KiB, and presigned URLs made from temporary
    credentials are long.
    """
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        "set -e; Xvfb :99 -screen 0 1280x800x24 -ac >/dev/null 2>&1 & export DISPLAY=:99; sleep 2; "
        f"echo {encoded} | base64 -d > /srv/meeting-guest.mjs; cd /srv && exec node /srv/meeting-guest.mjs"
    )


def guest_task_definition(
    vp_task_definition: dict[str, Any], family: str, script: str
) -> dict[str, Any]:
    """Derive the guest's task definition from the VP's.

    Same image, execution role (to pull it and write logs), size, platform and
    log group. No task role and no VP environment: the guest is a browser and
    nothing else.
    """
    vp_container = vp_task_definition["containerDefinitions"][0]
    log_configuration = json.loads(json.dumps(vp_container["logConfiguration"]))
    log_configuration.setdefault("options", {})["awslogs-stream-prefix"] = LOG_STREAM_PREFIX
    log_configuration.pop("secretOptions", None)
    definition: dict[str, Any] = {
        "family": family,
        "executionRoleArn": vp_task_definition["executionRoleArn"],
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": vp_task_definition.get("cpu") or "2048",
        "memory": vp_task_definition.get("memory") or "4096",
        "containerDefinitions": [
            {
                "name": CONTAINER_NAME,
                "image": vp_container["image"],
                "essential": True,
                "entryPoint": ["bash", "-c"],
                "command": [container_command(script)],
                "logConfiguration": log_configuration,
            }
        ],
    }
    if vp_task_definition.get("runtimePlatform"):
        definition["runtimePlatform"] = vp_task_definition["runtimePlatform"]
    return definition


def same_definition(registered: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """Whether a registered revision already matches, so none is added per run."""
    got = registered["containerDefinitions"][0]
    want = wanted["containerDefinitions"][0]
    return (
        got.get("image") == want["image"]
        and got.get("command") == want["command"]
        and registered.get("executionRoleArn") == wanted["executionRoleArn"]
        and not registered.get("taskRoleArn")
        and registered.get("cpu") == wanted["cpu"]
        and registered.get("memory") == wanted["memory"]
    )


def guest_family(stack: str) -> str:
    return f"{stack}-meeting-guest"[:255]


def append_command(commands: list[str], line: str) -> list[str]:
    """Commands are one per line, so a newline in a chat message would split it."""
    clean = " ".join(line.split())
    if not clean:
        raise ValueError("empty command")
    return [*commands, clean]


def log_outcome(messages: Iterable[str]) -> Optional[str]:
    """``in_call`` or ``gave_up`` once the guest's log says so, else None."""
    for message in messages:
        if IN_CALL in message:
            return "in_call"
        if GAVE_UP in message:
            return "gave_up"
    return None


def synthesize_conversation(
    polly: Any,
    turns: list[tuple[str, str]] = CONVERSATION,
    rate: int = 16000,
    lead_in_s: float = 60.0,
) -> bytes:
    """A mono 16 kHz WAV of the turns, with pauses, from Amazon Polly.

    Chrome plays the file from the moment the microphone opens, which is on
    Meet's pre-join screen, before the guest is admitted (and before any
    retries). ``lead_in_s`` of silence keeps the conversation for the call.
    """
    pause = b"\x00\x00" * int(rate * 0.8)
    pcm = bytearray(b"\x00\x00" * int(rate * lead_in_s))
    for voice, text in turns:
        response = polly.synthesize_speech(
            Text=text, VoiceId=voice, Engine="neural", OutputFormat="pcm", SampleRate=str(rate)
        )
        pcm += response["AudioStream"].read() + pause
    # Trailing quiet, so a player that loops is not mistaken for more speech.
    pcm += b"\x00\x00" * int(rate * 20)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return out.getvalue()


def _s3(session: boto3.session.Session) -> Any:
    """An S3 client whose presigned URLs work for KMS-encrypted objects.

    Those need Signature Version 4 on the bucket's regional endpoint; a URL
    for the global endpoint is answered with HTTP 400.
    """
    region = session.region_name
    return session.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com" if region else None,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def resolve_vp_resources(session: boto3.session.Session, stack: str) -> VpResources:
    """Find the VP nested stack's cluster, task definition, network and bucket."""
    cfn = session.client("cloudformation")
    vp_stack = cfn.describe_stack_resource(
        StackName=stack, LogicalResourceId="VIRTUALPARTICIPANTSTACK"
    )["StackResourceDetail"]["PhysicalResourceId"]
    described = cfn.describe_stacks(StackName=vp_stack)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}
    params = {
        p["ParameterKey"]: p.get("ParameterValue", "") for p in described.get("Parameters", [])
    }
    task_definition_arn = outputs["TaskDefinitionArn"]
    vp_td = session.client("ecs").describe_task_definition(taskDefinition=task_definition_arn)[
        "taskDefinition"
    ]
    env = {e["name"]: e["value"] for e in vp_td["containerDefinitions"][0].get("environment", [])}
    return VpResources(
        cluster_arn=outputs["ClusterArn"],
        task_definition_arn=task_definition_arn,
        subnets=[s for s in (params.get("PrivateSubnet1"), params.get("PrivateSubnet2")) if s],
        security_groups=[params["VPSecurityGroupId"]],
        bucket=env["RECORDINGS_BUCKET_NAME"],
    )


def start_guest(
    session: boto3.session.Session,
    stack: str,
    meet_url: str,
    name: str = "Conference Room 4",
    audio: Optional[bytes] = None,
    max_minutes: int = 90,
    lead_in_s: float = 60.0,
) -> GuestState:
    """Start a guest task and return its state. It joins in about a minute."""
    resources = resolve_vp_resources(session, stack)
    ecs = session.client("ecs")
    s3 = _s3(session)
    vp_td = ecs.describe_task_definition(taskDefinition=resources.task_definition_arn)[
        "taskDefinition"
    ]
    wanted = guest_task_definition(
        vp_td, guest_family(stack), GUEST_SCRIPT.read_text(encoding="utf-8")
    )
    try:
        current = ecs.describe_task_definition(taskDefinition=wanted["family"])["taskDefinition"]
    except ecs.exceptions.ClientException:
        current = None
    if current and same_definition(current, wanted):
        guest_td_arn = current["taskDefinitionArn"]
    else:
        guest_td_arn = ecs.register_task_definition(**wanted)["taskDefinition"]["taskDefinitionArn"]

    prefix = f"{S3_PREFIX}/{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    if audio is None:
        audio = synthesize_conversation(session.client("polly"), lead_in_s=lead_in_s)
    s3.put_object(Bucket=resources.bucket, Key=f"{prefix}/audio.wav", Body=audio)
    s3.put_object(Bucket=resources.bucket, Key=f"{prefix}/commands.txt", Body=b"")

    def presign(method: str, key: str) -> str:
        return s3.generate_presigned_url(
            method,
            Params={"Bucket": resources.bucket, "Key": f"{prefix}/{key}"},
            ExpiresIn=URL_TTL_SECONDS,
        )

    environment = {
        "MEET_URL": meet_url,
        "GUEST_NAME": name,
        "MAX_MINUTES": str(max_minutes),
        "AUDIO_URL": presign("get_object", "audio.wav"),
        "CMDS_URL": presign("get_object", "commands.txt"),
        "SHOT_URL": presign("put_object", "screenshot.png"),
    }
    task = ecs.run_task(
        cluster=resources.cluster_arn,
        taskDefinition=guest_td_arn,
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": resources.subnets,
                "securityGroups": resources.security_groups,
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": CONTAINER_NAME,
                    "environment": [{"name": k, "value": v} for k, v in environment.items()],
                }
            ]
        },
        startedBy="lma-meeting-guest",
    )
    if not task.get("tasks"):
        raise RuntimeError(f"run_task started nothing: {task.get('failures')}")
    return GuestState(
        stack=stack,
        region=session.region_name,
        cluster_arn=resources.cluster_arn,
        task_arn=task["tasks"][0]["taskArn"],
        bucket=resources.bucket,
        prefix=prefix,
        log_group=wanted["containerDefinitions"][0]["logConfiguration"]["options"]["awslogs-group"],
    )


def guest_log(session: boto3.session.Session, state: GuestState) -> list[str]:
    logs = session.client("logs")
    try:
        events = logs.get_log_events(
            logGroupName=state.log_group, logStreamName=state.log_stream, startFromHead=True
        )["events"]
    except logs.exceptions.ResourceNotFoundException:
        return []
    return [e["message"] for e in events]


def task_status(session: boto3.session.Session, state: GuestState) -> str:
    tasks = session.client("ecs").describe_tasks(cluster=state.cluster_arn, tasks=[state.task_arn])[
        "tasks"
    ]
    return tasks[0]["lastStatus"] if tasks else "MISSING"


def wait_until_joined(
    session: boto3.session.Session,
    state: GuestState,
    timeout_s: float = 600,
    poll_s: float = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """``in_call``, ``gave_up`` (Meet turned the guest away), ``stopped`` or ``timeout``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        outcome = log_outcome(guest_log(session, state))
        if outcome:
            return outcome
        if task_status(session, state) == "STOPPED":
            return "stopped"
        sleep(poll_s)
    return "timeout"


def send_command(session: boto3.session.Session, state: GuestState, line: str) -> GuestState:
    state.commands = append_command(state.commands, line)
    _s3(session).put_object(
        Bucket=state.bucket,
        Key=f"{state.prefix}/commands.txt",
        Body=("\n".join(state.commands) + "\n").encode("utf-8"),
    )
    return state


def fetch_screenshot(session: boto3.session.Session, state: GuestState) -> Optional[bytes]:
    s3 = _s3(session)
    try:
        return s3.get_object(Bucket=state.bucket, Key=f"{state.prefix}/screenshot.png")[
            "Body"
        ].read()
    except s3.exceptions.NoSuchKey:
        return None


def stop_guest(session: boto3.session.Session, state: GuestState, leave_first: bool = True) -> None:
    """Leave the meeting, stop the task and delete the run's S3 objects."""
    if leave_first and task_status(session, state) == "RUNNING":
        send_command(session, state, "leave")
        for _ in range(12):
            if task_status(session, state) == "STOPPED":
                break
            time.sleep(5)
    session.client("ecs").stop_task(
        cluster=state.cluster_arn, task=state.task_arn, reason="meeting guest done"
    )
    s3 = _s3(session)
    listed = s3.list_objects_v2(Bucket=state.bucket, Prefix=f"{state.prefix}/")
    keys = [{"Key": o["Key"]} for o in listed.get("Contents", []) if "Key" in o]
    if keys:
        s3.delete_objects(Bucket=state.bucket, Delete={"Objects": keys})


def _session(region: Optional[str]) -> boto3.session.Session:
    return boto3.session.Session(region_name=region) if region else boto3.session.Session()


def _load(path: Path) -> GuestState:
    return GuestState(**json.loads(path.read_text(encoding="utf-8")))


def _save(path: Path, state: GuestState) -> None:
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--state", type=Path, default=Path(".meeting-guest.json"))
    parser.add_argument("--region")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start", help="start a guest task")
    p.add_argument("--stack", required=True)
    p.add_argument("--meet-url", required=True)
    p.add_argument("--name", default="Conference Room 4")
    p.add_argument(
        "--audio", type=Path, help="WAV to play (default: a synthesized two-voice meeting)"
    )
    p.add_argument("--max-minutes", type=int, default=90)
    p.add_argument(
        "--lead-in",
        type=float,
        default=60.0,
        help="seconds of silence before the synthesized meeting (default 60)",
    )
    p = sub.add_parser("wait", help="wait until the guest is in the call")
    p.add_argument("--timeout", type=float, default=600)
    p = sub.add_parser("chat", help="send a chat message")
    p.add_argument("text")
    sub.add_parser("present", help="present a tab of slides")
    p = sub.add_parser("shot", help="save a screenshot of the guest's window")
    p.add_argument("out", type=Path)
    sub.add_parser("leave", help="leave, stop the task and delete its S3 objects")
    args = parser.parse_args(argv)

    session = _session(args.region)
    if args.cmd == "start":
        audio = args.audio.read_bytes() if args.audio else None
        state = start_guest(
            session, args.stack, args.meet_url, args.name, audio, args.max_minutes, args.lead_in
        )
        _save(args.state, state)
        print(f"started {state.task_id}; state in {args.state}")
        return 0
    state = _load(args.state)
    session = _session(args.region or state.region)
    if args.cmd == "wait":
        outcome = wait_until_joined(session, state, args.timeout)
        print(outcome)
        return 0 if outcome == "in_call" else 1
    if args.cmd in ("chat", "present"):
        line = f"chat {args.text}" if args.cmd == "chat" else "present"
        _save(args.state, send_command(session, state, line))
        return 0
    if args.cmd == "shot":
        before = fetch_screenshot(session, state)
        _save(args.state, send_command(session, state, "shot"))
        for _ in range(12):
            time.sleep(5)
            image = fetch_screenshot(session, state)
            if image and image != before:
                args.out.write_bytes(image)
                print(f"wrote {args.out}")
                return 0
        print("no new screenshot", file=sys.stderr)
        return 1
    stop_guest(session, state)
    args.state.unlink(missing_ok=True)
    print("guest stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
