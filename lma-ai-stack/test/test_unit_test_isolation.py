# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The two pieces that keep unit tests off AWS: the guard and the strands stand-in.

* ``scripts/testing/no_real_aws.py`` fails a test that sends a request to AWS,
  including one the code under test catches, and lets a test marked
  ``real_aws`` through. The swallowed case is the one that matters, so it is run
  in a pytest session of its own and its outcome read back.
* ``scripts/testing/strands_stand_in.py`` has to offer every name the Lambda code
  imports from ``strands``: a name it lacks sends the meeting assistant's handler
  to its direct-Bedrock fallback. Held equal to the code's import lines.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import boto3
import no_real_aws
import pytest
import strands_stand_in

ROOT = Path(__file__).resolve().parents[2]
GUARD_DIR = ROOT / "scripts" / "testing"
STRANDS_USERS = (ROOT / "lma-meetingassist-setup-stack" / "src",)


# ── the guard ───────────────────────────────────────────────────────────────


def test_a_request_to_aws_is_refused():
    client = boto3.client("sts", region_name="us-east-1")
    blocked = no_real_aws._STATE["blocked"]
    before = len(blocked)
    with pytest.raises(no_real_aws.RealAwsCallBlocked, match="sts"):
        client.get_caller_identity()
    assert len(blocked) == before + 1
    # Recorded, so the autouse fixture would fail this test; taken back because
    # here the refusal is the expected outcome.
    del blocked[before:]


@pytest.mark.skipif(
    os.environ.get(no_real_aws.REAL_CREDENTIALS_ENV_VAR) == "1",
    reason=f"{no_real_aws.REAL_CREDENTIALS_ENV_VAR}=1 keeps the caller's own credentials",
)
def test_the_session_signs_with_fake_credentials():
    credentials = boto3.Session().get_credentials()
    assert credentials.access_key == no_real_aws.FAKE_CREDENTIALS["AWS_ACCESS_KEY_ID"]


def _run_session(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.append({str(GUARD_DIR)!r})
            from no_real_aws import *  # noqa: F403
            from no_real_aws import activate
            activate()
            """
        )
    )
    (tmp_path / "test_inner.py").write_text(textwrap.dedent(body))
    return subprocess.run(  # noqa: S603 -- this interpreter, fixed arguments
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )


def test_a_request_the_code_swallows_still_fails_the_test(tmp_path):
    result = _run_session(
        tmp_path,
        """
        import boto3

        def test_swallowed():
            try:
                boto3.client("sts", region_name="us-east-1").get_caller_identity()
            except Exception:
                pass
        """,
    )
    assert result.returncode != 0, result.stdout
    assert "this test sent requests to AWS" in result.stdout
    assert "sts.us-east-1.amazonaws.com" in result.stdout


def test_a_marked_test_is_let_through(tmp_path):
    result = _run_session(
        tmp_path,
        """
        import no_real_aws
        import pytest

        @pytest.mark.real_aws
        def test_marked():
            assert no_real_aws._STATE["allowed"] is True

        def test_unmarked():
            assert no_real_aws._STATE["allowed"] is False
        """,
    )
    assert result.returncode == 0, result.stdout


# ── the strands stand-in ────────────────────────────────────────────────────


def _imported_from_strands() -> dict[str, set[str]]:
    imported: dict[str, set[str]] = {}
    for directory in STRANDS_USERS:
        for path in sorted(directory.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == (
                    "strands"
                ):
                    imported.setdefault(node.module, set()).update(a.name for a in node.names)
    return imported


def test_the_stand_in_offers_exactly_what_the_code_imports_from_strands():
    imported = _imported_from_strands()
    assert imported, "no `from strands ... import` found; has the code moved?"
    offered = {module: set(names) for module, names in strands_stand_in.IMPORTED_NAMES.items()}
    assert offered == imported


def test_the_stand_in_is_importable_the_way_the_code_imports_it(monkeypatch):
    strands_stand_in.install(monkeypatch)
    from strands import tool  # noqa: PLC0415
    from strands.models import BedrockModel, CacheConfig  # noqa: PLC0415

    @tool(context=True)
    def decorated():
        return "body"

    assert decorated() == "body"
    assert BedrockModel(model_id="m").config == {"model_id": "m"}
    assert CacheConfig(strategy="auto").strategy == "auto"
    with pytest.raises(AssertionError, match="stand-in strands.Agent"):
        sys.modules["strands"].Agent()


def test_the_stand_in_is_removed_after_its_block():
    before = sys.modules.get("strands")
    with strands_stand_in.installed() as stub:
        assert sys.modules["strands"] is stub
    assert sys.modules.get("strands") is before
