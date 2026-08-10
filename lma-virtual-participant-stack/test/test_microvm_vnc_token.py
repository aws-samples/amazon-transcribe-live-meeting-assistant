"""Unit tests for the MicroVM VNC token Lambda's response shape.

Why this file exists: the VNC viewer failed with "Failed to connect:
{data: {createMicrovmVncToken: null}, errors: [...]}" even though the Lambda
logged "Minted VNC token" successfully. The mutation returned

    expiresAt: "None"

because the code did `str(response.get("expiresAt"))` — but
CreateMicrovmAuthToken returns ONLY `authToken` (verified against the service
model). The GraphQL field is `AWSDateTime!`, so AppSync could not serialize the
string "None" and nulled the ENTIRE parent object:

    Can't serialize value (/createMicrovmVncToken/expiresAt) :
    Unable to serialize `None` as a valid DateTime Object.

A valid auth token was discarded over a metadata field. These tests assert the
Lambda emits a real ISO-8601 UTC timestamp that AppSync can serialize.

No AWS calls: the MicroVM client and DynamoDB lookup are stubbed.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOKEN_DIR = REPO / "lma-ai-stack" / "source" / "lambda_functions" / "microvm_vnc_token"
SCHEMA = REPO / "lma-ai-stack" / "source" / "appsync" / "schema.graphql"
sys.path.insert(0, str(TOKEN_DIR))

# AWSDateTime requires an offset; AppSync accepts the "Z" form.
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _code_only(path: Path) -> str:
    """Source with comments stripped.

    These assertions are about what the code DOES; the file deliberately
    documents the old broken expression in a comment, and matching that would
    make the test fail on its own explanation.
    """
    lines = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_schema_still_declares_expires_at_non_null() -> None:
    """The whole failure mode depends on this being non-null.

    If `expiresAt` were nullable, a bad value would null just that field. Because
    it is `AWSDateTime!`, AppSync nulls the parent object and the viewer loses
    the auth token entirely. Pinned so the reasoning above stays valid.
    """
    schema = SCHEMA.read_text()
    block = schema[schema.index("type MicrovmVncToken") :]
    block = block[: block.index("}")]
    assert "authToken: String!" in block
    assert "expiresAt: AWSDateTime!" in block


def test_source_does_not_read_expires_at_from_the_api_response() -> None:
    """CreateMicrovmAuthToken has exactly one output member: authToken.

    Reading a non-existent `expiresAt` is what produced the string "None".
    """
    src = _code_only(TOKEN_DIR / "index.py")
    assert 'response.get("expiresAt")' not in src, (
        "CreateMicrovmAuthToken returns only authToken; expiresAt must be "
        "computed from TOKEN_TTL_MINUTES"
    )
    assert "TOKEN_TTL_MINUTES" in src


def test_handler_returns_a_serializable_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end shape check through the real handler, AWS calls stubbed."""
    monkeypatch.setenv("VP_TABLE_NAME", "vp-table")
    monkeypatch.setenv("VP_TASK_REGISTRY_TABLE_NAME", "registry-table")
    import index as token_index

    class _FakeDynamo:
        def get_item(self, TableName, Key):  # noqa: N803 - boto3 kwarg names
            if TableName == "vp-table":
                return {"Item": {"Owner": {"S": "bob@example.com"}}}
            return {"Item": {"microvmId": {"S": "microvm-abc"}}}

    class _FakeMicrovms:
        def create_microvm_auth_token(self, **kwargs):
            # Exactly what the real API returns -- note: no expiresAt.
            assert kwargs["expiration_in_minutes"] == token_index.TOKEN_TTL_MINUTES
            return {"authToken": {"X-aws-proxy-auth": "a.token.value"}}

    monkeypatch.setattr(token_index, "dynamodb", _FakeDynamo())
    monkeypatch.setattr(token_index, "microvms", _FakeMicrovms())

    result = token_index.lambda_handler(
        {
            "arguments": {"vpId": "vp-1"},
            "identity": {"username": "bob@example.com", "claims": {}},
        },
        None,
    )

    assert result["authToken"] == "a.token.value"
    assert ISO_Z.match(result["expiresAt"]), (
        f"expiresAt must be an AppSync-serializable AWSDateTime, got "
        f"{result['expiresAt']!r}"
    )
    # Must be in the future, or the viewer would treat the token as expired.
    parsed = datetime.fromisoformat(result["expiresAt"].replace("Z", "+00:00"))
    assert parsed > datetime.now(timezone.utc)


def test_expires_at_is_never_the_string_none() -> None:
    """A regression guard on the exact observed bad value."""
    src = _code_only(TOKEN_DIR / "index.py")
    assert "str(expires_at)" not in src
