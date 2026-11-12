# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for scripts/meeting_guest (no AWS)."""

from __future__ import annotations

import base64
import importlib.util
import io
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "meeting_guest", ROOT / "scripts" / "meeting_guest" / "meeting_guest.py"
)
assert _spec and _spec.loader
mg = importlib.util.module_from_spec(_spec)
sys.modules["meeting_guest"] = mg  # dataclasses look their module up here
_spec.loader.exec_module(mg)

VP_TD = {
    "taskDefinitionArn": "arn:aws:ecs:us-west-2:111122223333:task-definition/vp:7",
    "taskRoleArn": "arn:aws:iam::111122223333:role/vp-task",
    "executionRoleArn": "arn:aws:iam::111122223333:role/vp-exec",
    "cpu": "2048",
    "memory": "4096",
    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    "containerDefinitions": [
        {
            "name": "scribe",
            "image": "111122223333.dkr.ecr.us-west-2.amazonaws.com/vp:latest",
            "environment": [{"name": "RECORDINGS_BUCKET_NAME", "value": "recordings"}],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": "/vp/fargate",
                    "awslogs-stream-prefix": "lmavirtualparticipant",
                },
                "secretOptions": [],
            },
        }
    ],
}


def _guest_td() -> dict:
    return mg.guest_task_definition(VP_TD, "LMA-meeting-guest", "console.log('hi');\n")


def test_guest_task_definition_has_no_task_role_and_no_vp_environment():
    td = _guest_td()
    assert "taskRoleArn" not in td
    container = td["containerDefinitions"][0]
    assert "environment" not in container
    assert container["name"] == mg.CONTAINER_NAME


def test_guest_task_definition_keeps_the_vp_image_execution_role_size_and_platform():
    td = _guest_td()
    container = td["containerDefinitions"][0]
    assert container["image"] == VP_TD["containerDefinitions"][0]["image"]
    assert td["executionRoleArn"] == VP_TD["executionRoleArn"]
    assert (td["cpu"], td["memory"]) == ("2048", "4096")
    assert td["runtimePlatform"] == VP_TD["runtimePlatform"]


def test_guest_logs_go_to_the_vp_log_group_under_their_own_prefix():
    options = _guest_td()["containerDefinitions"][0]["logConfiguration"]["options"]
    assert options["awslogs-group"] == "/vp/fargate"
    assert options["awslogs-stream-prefix"] == mg.LOG_STREAM_PREFIX
    # The VP's own definition is left as it was.
    assert VP_TD["containerDefinitions"][0]["logConfiguration"]["options"][
        "awslogs-stream-prefix"
    ] == ("lmavirtualparticipant")


def test_container_command_carries_the_script_intact():
    script = 'const q = "it\'s"; console.log(`$HOME ${q}`);\n'
    command = mg.container_command(script)
    encoded = command.split("echo ", 1)[1].split(" | base64", 1)[0]
    assert base64.b64decode(encoded).decode("utf-8") == script
    assert "exec node /srv/meeting-guest.mjs" in command


def test_the_shipped_guest_script_fits_in_a_task_definition():
    command = mg.container_command(mg.GUEST_SCRIPT.read_text(encoding="utf-8"))
    assert len(command) < 32 * 1024


def test_a_registered_revision_is_reused_only_when_it_matches():
    wanted = _guest_td()
    registered = {**wanted, "taskDefinitionArn": "arn:...:LMA-meeting-guest:3"}
    assert mg.same_definition(registered, wanted)
    changed = mg.guest_task_definition(VP_TD, "LMA-meeting-guest", "console.log('changed');\n")
    assert not mg.same_definition(registered, changed)
    with_role = {**registered, "taskRoleArn": "arn:aws:iam::111122223333:role/any"}
    assert not mg.same_definition(with_role, wanted)


def test_commands_are_one_line_each():
    commands = mg.append_command([], "chat LMA what was\njust discussed?")
    assert commands == ["chat LMA what was just discussed?"]
    assert mg.append_command(commands, "leave") == [*commands, "leave"]
    with pytest.raises(ValueError):
        mg.append_command(commands, "   \n")


def test_log_outcome_reads_the_guest_markers():
    assert mg.log_outcome(["10:00:01 [guest] join clicked"]) is None
    assert mg.log_outcome(["[guest] join clicked", "[guest] IN CALL"]) == "in_call"
    assert mg.log_outcome(["[guest] not admitted", "[guest] GAVE UP"]) == "gave_up"


class _FakePolly:
    def __init__(self):
        self.calls = []

    def synthesize_speech(self, **kwargs):
        self.calls.append(kwargs)
        return {"AudioStream": io.BytesIO(b"\x01\x00" * 1600)}


def test_synthesized_conversation_is_a_mono_16k_wav_of_every_turn():
    polly = _FakePolly()
    audio = mg.synthesize_conversation(polly, [("Ruth", "Hello."), ("Stephen", "Hi.")])
    assert [c["VoiceId"] for c in polly.calls] == ["Ruth", "Stephen"]
    with wave.open(io.BytesIO(audio)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        assert w.getnframes() > 2 * 1600


def test_guest_state_names_its_log_stream_after_the_task():
    state = mg.GuestState(
        stack="LMA",
        region="us-west-2",
        cluster_arn="arn:cluster",
        task_arn="arn:aws:ecs:us-west-2:111122223333:task/cluster/0123abcd",
        bucket="recordings",
        prefix="meeting-guest/x",
        log_group="/vp/fargate",
    )
    assert state.log_stream == "meeting-guest/guest/0123abcd"


def test_the_start_command_passes_its_options_to_start_guest(monkeypatch, tmp_path):
    seen = {}

    def fake_start(session, stack, meet_url, name, audio, max_minutes, lead_in_s):
        seen.update(
            stack=stack, meet_url=meet_url, name=name, max_minutes=max_minutes, lead_in_s=lead_in_s
        )
        return mg.GuestState(
            stack=stack,
            region="us-west-2",
            cluster_arn="arn:cluster",
            task_arn="arn:aws:ecs:us-west-2:111122223333:task/cluster/abc",
            bucket="recordings",
            prefix="meeting-guest/x",
            log_group="/vp/fargate",
        )

    monkeypatch.setattr(mg, "start_guest", fake_start)
    state = tmp_path / "guest.json"
    argv = ["--state", str(state), "--region", "us-west-2", "start", "--stack", "LMA"]
    argv += [
        "--meet-url",
        "https://meet.google.com/abc-defg-hij",
        "--lead-in",
        "5",
        "--max-minutes",
        "8",
    ]
    assert mg.main(argv) == 0
    assert seen == {
        "stack": "LMA",
        "meet_url": "https://meet.google.com/abc-defg-hij",
        "name": "Conference Room 4",
        "max_minutes": 8,
        "lead_in_s": 5.0,
    }
    assert state.exists()
