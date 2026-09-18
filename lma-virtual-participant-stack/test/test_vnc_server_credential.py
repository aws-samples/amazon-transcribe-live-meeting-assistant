# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""How entrypoint.sh starts x11vnc, for each place the VP runs.

The same script boots the framebuffer in three quite different situations, and
the right flags differ in each:

* **Deployed on ECS.** The ALB reaches websockify on 5901 only, so x11vnc binds
  the loopback interface and asks for a credential. The credential is generated
  per boot and published on this task's own VP record, from which the
  authenticated ``createVncEdgeToken`` resolver hands it to viewers that it has
  already checked against that record.
* **Deployed as a MicroVM pre-snapshot stack** (``STACK_ONLY=true``). This runs
  at image-build time, where there is no per-meeting VP record to publish a
  credential to, so the framebuffer stays loopback-only and the per-session,
  port-scoped MicroVM auth token guards the noVNC port instead.
* **Local development** (``LOCAL_TEST=true``, set by local-test.sh). 5900 is
  published to the developer's own machine on purpose, so a desktop VNC client
  can attach as docs/virtual-participant-local-dev.md describes.

These are shell branches, so the tests below actually RUN the relevant section of
the script with stub ``x11vnc`` and ``aws`` executables on PATH and inspect the
arguments each received. A static substring check would not catch a branch that
selects the wrong array.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
ENTRYPOINT = BACKEND / "entrypoint.sh"

START_MARKER = "VNC_BIND_ARGS=("
END_MARKER = "-noxdamage"

STUB_X11VNC = """#!/bin/bash
# Two roles: write a credential file, or "serve". Record which.
if [ "$1" = "-storepasswd" ]; then
    printf 'stored-credential' > "$3"
    exit 0
fi
printf '%s\\n' "$@" > "$X11VNC_ARGS"
"""

STUB_AWS = """#!/bin/bash
printf '%s\\n' "$@" > "$AWS_ARGS"
"""


def vnc_startup_snippet() -> str:
    """The x11vnc flag selection and launch, lifted out of entrypoint.sh.

    Run rather than pattern-matched, so a mis-selected array fails here. The
    launch is de-backgrounded (the trailing ``\\`` and the redirect are dropped)
    so the stub has finished recording by the time the snippet returns.
    """
    lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(START_MARKER))
    end = next(i for i, line in enumerate(lines) if line.strip().startswith(END_MARKER))
    assert start < end, "expected the flag selection to precede the x11vnc launch"
    body = lines[start:end] + [lines[end].rstrip("\\ ")]
    return "\n".join(body) + "\n"


@pytest.fixture(name="run_startup")
def run_startup_fixture(tmp_path: Path):
    """Run the snippet with the given environment; return the recorded arguments."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name, source in (("x11vnc", STUB_X11VNC), ("aws", STUB_AWS)):
        stub = stub_dir / name
        stub.write_text(source, encoding="utf-8")
        stub.chmod(0o755)

    x11vnc_args = tmp_path / "x11vnc.args"
    aws_args = tmp_path / "aws.args"
    script = tmp_path / "snippet.sh"
    script.write_text(vnc_startup_snippet(), encoding="utf-8")

    def run(**env_overrides):
        env = {
            "PATH": f"{stub_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "X11VNC_ARGS": str(x11vnc_args),
            "AWS_ARGS": str(aws_args),
            "AWS_REGION": "us-east-1",
        }
        env.update({k: v for k, v in env_overrides.items() if v is not None})
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
        )
        return {
            "x11vnc": x11vnc_args.read_text().split() if x11vnc_args.exists() else [],
            "aws": aws_args.read_text().splitlines() if aws_args.exists() else [],
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    return run


DEPLOYED_ECS = {
    "VIRTUAL_PARTICIPANT_ID": "vp-test-1",
    "VP_TABLE_NAME": "vp-table",
}


def test_entrypoint_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(ENTRYPOINT)], check=True)


# --------------------------------------------------------------------------
# Deployed on ECS
# --------------------------------------------------------------------------


def test_ecs_task_requires_a_credential(run_startup) -> None:
    recorded = run_startup(**DEPLOYED_ECS)
    assert "-rfbauth" in recorded["x11vnc"]
    assert "-nopw" not in recorded["x11vnc"]


def test_ecs_task_binds_the_loopback_interface(run_startup) -> None:
    """websockify on 5901 is the intended route to the framebuffer."""
    recorded = run_startup(**DEPLOYED_ECS)
    assert "-localhost" in recorded["x11vnc"]


def test_ecs_credential_is_published_on_this_tasks_own_record(run_startup) -> None:
    recorded = run_startup(**DEPLOYED_ECS)
    aws_args = recorded["aws"]
    assert aws_args, "the credential must be published for viewers to obtain it"
    assert "update-item" in aws_args
    assert "vp-table" in aws_args
    joined = " ".join(aws_args)
    assert "vncPassword" in joined
    assert "vp-test-1" in joined, "the credential belongs to one VP record, not a shared one"


def test_ecs_credential_is_published_before_x11vnc_starts_serving(run_startup) -> None:
    """Otherwise a viewer can meet a server whose credential the record lacks."""
    snippet = vnc_startup_snippet()
    assert snippet.index("update-item") < snippet.index("-rfbport"), (
        "publish the credential before launching x11vnc"
    )
    run_startup(**DEPLOYED_ECS)  # and the ordering really is reachable


def test_ecs_credential_is_randomly_generated_per_boot(run_startup) -> None:
    first = run_startup(**DEPLOYED_ECS)["aws"]
    second = run_startup(**DEPLOYED_ECS)["aws"]
    assert first != second, "each boot must generate its own credential"


def test_ecs_credential_fits_the_classic_rfb_scheme(run_startup) -> None:
    """Classic RFB truncates to 8 characters, so generate exactly that many.

    A longer string would be silently cut, and the viewer would present the full
    value and never connect.
    """
    published = " ".join(run_startup(**DEPLOYED_ECS)["aws"])
    match = re.search(r'":p":\{"S":"([^"]*)"\}', published)
    assert match, f"expected a vncPassword value in: {published}"
    assert len(match.group(1)) == 8
    assert match.group(1).isalnum()


def test_the_credential_is_not_echoed_to_the_task_log(run_startup) -> None:
    recorded = run_startup(**DEPLOYED_ECS)
    published = " ".join(recorded["aws"])
    match = re.search(r'":p":\{"S":"([^"]*)"\}', published)
    assert match
    secret = match.group(1)
    assert secret not in recorded["stdout"]
    assert secret not in recorded["stderr"]


def test_the_credential_file_is_not_world_readable(run_startup) -> None:
    snippet = vnc_startup_snippet()
    assert "chmod 600" in snippet


def test_x11vnc_still_starts_if_the_credential_cannot_be_published(
    run_startup, tmp_path: Path
) -> None:
    """A VNC server that never starts is worse than one a viewer cannot reach.

    The task's own logs say what happened; the meeting itself does not depend on
    the live view.
    """
    failing_aws = tmp_path / "bin" / "aws"
    recorded = run_startup(**DEPLOYED_ECS)
    assert recorded["x11vnc"], "x11vnc must be launched"
    failing_aws.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    failing_aws.chmod(0o755)
    recorded = run_startup(**DEPLOYED_ECS)
    assert "-rfbport" in recorded["x11vnc"]
    assert "5900" in recorded["x11vnc"]


# --------------------------------------------------------------------------
# MicroVM pre-snapshot stack
# --------------------------------------------------------------------------


def test_presnapshot_stack_stays_loopback_only(run_startup) -> None:
    """Built before any meeting exists, so there is no record to publish to.

    The noVNC port is reached with a per-session, port-scoped MicroVM auth token
    instead (see MicrovmVncTokenFunction in the AI stack).
    """
    recorded = run_startup(STACK_ONLY="true", VP_LAUNCH_TYPE="MICROVM")
    assert "-localhost" in recorded["x11vnc"]
    assert recorded["aws"] == [], "there is no per-meeting record at image-build time"


def test_presnapshot_stack_does_not_bake_a_credential_into_the_image(run_startup) -> None:
    """A credential captured in the snapshot would be shared by every launch."""
    recorded = run_startup(STACK_ONLY="true", VP_LAUNCH_TYPE="MICROVM")
    assert "-rfbauth" not in recorded["x11vnc"]


# --------------------------------------------------------------------------
# Local development
# --------------------------------------------------------------------------


def test_local_test_leaves_the_published_port_reachable(run_startup) -> None:
    """local-test.sh publishes -p 5900:5900 for a desktop VNC client.

    Binding the loopback interface inside the container would make the published
    port answer nothing, breaking the documented local-dev workflow.
    """
    recorded = run_startup(LOCAL_TEST="true", **DEPLOYED_ECS)
    assert "-localhost" not in recorded["x11vnc"]
    assert "-nopw" in recorded["x11vnc"]
    assert recorded["aws"] == []


# --------------------------------------------------------------------------
# Invariants that hold everywhere
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        DEPLOYED_ECS,
        {"STACK_ONLY": "true"},
        {"LOCAL_TEST": "true"},
        {},  # no VP identity available at all
    ],
    ids=["ecs", "presnapshot", "local", "no-identity"],
)
def test_x11vnc_is_always_launched_with_exactly_one_auth_mode(run_startup, env) -> None:
    """x11vnc refuses to start with neither -nopw nor -rfbauth."""
    recorded = run_startup(**env)
    modes = [flag for flag in recorded["x11vnc"] if flag in ("-nopw", "-rfbauth")]
    assert len(modes) == 1, f"expected one auth mode, got {modes}"


def test_the_framebuffer_port_is_unchanged(run_startup) -> None:
    """websockify connects to localhost:5900; the two must agree."""
    recorded = run_startup(**DEPLOYED_ECS)
    args = recorded["x11vnc"]
    assert args[args.index("-rfbport") + 1] == "5900"
    assert "localhost:5900" in ENTRYPOINT.read_text(encoding="utf-8")
