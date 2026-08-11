# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the ASR MicroVM session launcher (no AWS calls).

The launcher is called from the transcriber on the meeting's critical path, so the
contracts pinned here are: never raise (the caller falls back to Amazon Transcribe
on a returned failure), never leave a MicroVM running after a failed acquire, and
never suspend a MicroVM mid-meeting (a suspended VM stops transcribing).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("MICROVM_IMAGE_ARN", "arn:aws:lambda:us-east-1:1234:microvm-image/asr")
os.environ.setdefault("EXECUTION_ROLE_ARN", "arn:aws:iam::1234:role/asr-exec")
os.environ.setdefault("MAX_MEETING_SECONDS", "14400")

sys.path.insert(0, str(Path(__file__).parent))

import index  # noqa: E402
from microvm_client import MicrovmError  # noqa: E402

TOKEN_RESPONSE = {"authToken": {"X-aws-proxy-auth": "jwe-token"}}


@pytest.fixture(autouse=True)
def no_sleep():
    with mock.patch.object(index.time, "sleep"):
        yield


def fake_microvms(**overrides) -> mock.Mock:
    client = mock.Mock()
    client.run_microvm.return_value = {
        "microvmId": "mvm-1",
        "endpoint": "mvm-1.lambda-microvms.example",
        "state": "RUNNING",
    }
    client.get_microvm.return_value = {"state": "RUNNING", "endpoint": "mvm-1.lambda-microvms.example"}
    client.create_microvm_auth_token.return_value = TOKEN_RESPONSE
    client.terminate_microvm.return_value = {"state": "TERMINATED"}
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


def test_acquire_returns_the_endpoint_and_a_port_scoped_token() -> None:
    client = fake_microvms()
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "acquire", "callId": "Team sync - 2026-08-11"}, None)

    assert result["ok"] is True
    assert result["microvmId"] == "mvm-1"
    assert result["endpoint"] == "mvm-1.lambda-microvms.example"
    assert result["authToken"] == "jwe-token"
    assert result["port"] == 8080
    client.create_microvm_auth_token.assert_called_once()
    assert client.create_microvm_auth_token.call_args.kwargs["allowed_ports"] == [{"port": 8080}]


def test_acquire_never_lets_a_microvm_suspend_mid_meeting() -> None:
    client = fake_microvms()
    with mock.patch.object(index, "microvms", client):
        index.lambda_handler({"action": "acquire", "callId": "c"}, None)

    kwargs = client.run_microvm.call_args.kwargs
    # A suspended MicroVM stops transcribing, so it would silently miss meeting
    # content.
    assert kwargs["idlePolicy"]["autoResumeEnabled"] is False
    assert kwargs["idlePolicy"]["suspendedDurationSeconds"] == 0
    assert kwargs["maximumDurationInSeconds"] == 14400
    assert kwargs["ingressNetworkConnectors"] == [index.INGRESS_CONNECTOR]
    assert kwargs["egressNetworkConnectors"] == [index.EGRESS_CONNECTOR]
    assert kwargs["executionRoleArn"] == os.environ["EXECUTION_ROLE_ARN"]


def test_acquire_uses_a_deterministic_client_token_for_the_call() -> None:
    call_id = "Weekly review - 2026-08-11T10:00:00Z"
    token = index._client_token(call_id)

    assert token == index._client_token(call_id)
    assert len(token) == 32
    assert all(character in "0123456789abcdef" for character in token)


def test_acquire_waits_for_running_before_reporting_success() -> None:
    client = fake_microvms()
    client.run_microvm.return_value = {"microvmId": "mvm-1", "endpoint": "e", "state": "PENDING"}
    client.get_microvm.side_effect = [
        {"state": "STARTING"},
        {"state": "RUNNING"},
    ]
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "acquire", "callId": "c"}, None)

    assert result["ok"] is True
    assert client.get_microvm.call_count == 2


def test_acquire_terminates_a_microvm_that_never_reaches_running() -> None:
    client = fake_microvms()
    client.run_microvm.return_value = {"microvmId": "mvm-1", "endpoint": "e", "state": "PENDING"}
    client.get_microvm.return_value = {"state": "FAILED"}
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "acquire", "callId": "c"}, None)

    assert result["ok"] is False
    assert "state=FAILED" in result["reason"]
    client.terminate_microvm.assert_called_once_with("mvm-1")


def test_acquire_terminates_the_microvm_when_the_token_cannot_be_minted() -> None:
    client = fake_microvms()
    client.create_microvm_auth_token.side_effect = MicrovmError("denied", status=403)
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "acquire", "callId": "c"}, None)

    assert result["ok"] is False
    assert "CreateMicrovmAuthToken failed" in result["reason"]
    client.terminate_microvm.assert_called_once_with("mvm-1")


def test_acquire_reports_a_quota_failure_instead_of_raising() -> None:
    client = fake_microvms()
    client.run_microvm.side_effect = MicrovmError("Too many microvms", status=429)
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "acquire", "callId": "c"}, None)

    assert result["ok"] is False
    assert "Too many microvms" in result["reason"]


def test_acquire_reports_an_unexpected_error_instead_of_raising() -> None:
    client = fake_microvms()
    client.run_microvm.side_effect = RuntimeError("boom")
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "acquire", "callId": "c"}, None)

    assert result["ok"] is False
    assert "boom" in result["reason"]


def test_acquire_requires_a_call_id() -> None:
    with mock.patch.object(index, "microvms", fake_microvms()) as client:
        result = index.lambda_handler({"action": "acquire"}, None)

    assert result["ok"] is False
    client.run_microvm.assert_not_called()


def test_acquire_fails_when_there_is_no_endpoint_to_connect_to() -> None:
    client = fake_microvms()
    client.run_microvm.return_value = {"microvmId": "mvm-1", "endpoint": "", "state": "RUNNING"}
    client.get_microvm.return_value = {"state": "RUNNING", "endpoint": ""}
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "acquire", "callId": "c"}, None)

    assert result["ok"] is False
    assert "no endpoint" in result["reason"]
    client.terminate_microvm.assert_called_once_with("mvm-1")


def test_token_mints_a_fresh_token_for_a_reconnect() -> None:
    client = fake_microvms()
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "token", "microvmId": "mvm-1"}, None)

    assert result["ok"] is True
    assert result["authToken"] == "jwe-token"
    assert result["expiresAt"].endswith("Z")


def test_token_requires_a_microvm_id() -> None:
    with mock.patch.object(index, "microvms", fake_microvms()):
        result = index.lambda_handler({"action": "token"}, None)

    assert result["ok"] is False


def test_release_terminates_the_microvm() -> None:
    client = fake_microvms()
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "release", "microvmId": "mvm-1"}, None)

    assert result["ok"] is True
    client.terminate_microvm.assert_called_once_with("mvm-1")


def test_releasing_an_already_gone_microvm_is_a_success() -> None:
    client = fake_microvms()
    client.terminate_microvm.side_effect = MicrovmError("not found", status=404)
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "release", "microvmId": "mvm-1"}, None)

    assert result["ok"] is True


def test_a_failed_release_is_reported_not_raised() -> None:
    client = fake_microvms()
    client.terminate_microvm.side_effect = MicrovmError("throttled", status=429)
    with mock.patch.object(index, "microvms", client):
        result = index.lambda_handler({"action": "release", "microvmId": "mvm-1"}, None)

    assert result["ok"] is False
    assert "throttled" in result["reason"]


def test_an_unknown_action_is_reported() -> None:
    result = index.lambda_handler({"action": "explode"}, None)

    assert result["ok"] is False
    assert "unknown action" in result["reason"]


def test_the_meeting_ceiling_never_exceeds_the_service_limit() -> None:
    assert index.MAX_MEETING_SECONDS <= 28800
