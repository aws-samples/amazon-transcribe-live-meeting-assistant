# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for how the MicroVM launcher supplies integration credentials.

On the ECS launch types the ECS agent resolves the task definition's `Secrets`
entries with the task execution role and sets the named environment variables
in the container. A MicroVM has no ECS agent, so the launcher does the same
resolution before staging the container's configuration — which is what keeps
the container reading `process.env.ELEVENLABS_API_KEY` (and the other three)
unchanged on both paths.

Three properties are asserted: the values reach the staged configuration under
the names the task definition gave them; a deployment that configured no
integration makes no Secrets Manager call at all; and one unreadable secret
costs that one feature rather than the whole meeting.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

LAUNCHER_DIR = (
    Path(__file__).resolve().parents[1] / "lambda_functions" / "microvm_launcher"
)

TASK_DEFINITION_ARN = (
    "arn:aws:ecs:us-west-2:123456789012:task-definition/lmaVirtualParticipantTaskDef:7"
)
SECRET_ARN_PREFIX = "arn:aws:secretsmanager:us-west-2:123456789012:secret:lma/integrations/"


def _load(name: str, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch):
    """A fresh launcher module, with its sibling `microvm_client`.

    Fresh per test because the launcher caches the static configuration for the
    life of the execution environment, which is part of what is under test.
    Loaded by path for the reason test_image_version_pruner.py gives: several
    deployment packages ship a file of that name.
    """
    monkeypatch.setenv("TASK_DEFINITION_ARN", TASK_DEFINITION_ARN)
    # The launcher builds its DynamoDB and MicroVM clients at import, which
    # needs a region to be resolvable without reaching AWS.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    previous = sys.modules.get("microvm_client")
    sys.path.insert(0, str(LAUNCHER_DIR))
    try:
        _load("microvm_client", LAUNCHER_DIR / "microvm_client.py")
        yield _load("microvm_launcher_under_test", LAUNCHER_DIR / "index.py")
    finally:
        sys.path.remove(str(LAUNCHER_DIR))
        sys.modules.pop("microvm_launcher_under_test", None)
        if previous is None:
            sys.modules.pop("microvm_client", None)
        else:
            sys.modules["microvm_client"] = previous


SECRET_ENTRIES = [
    {"name": "ELEVENLABS_API_KEY", "valueFrom": f"{SECRET_ARN_PREFIX}elevenlabs-api-key"},
    {"name": "SIMLI_API_KEY", "valueFrom": f"{SECRET_ARN_PREFIX}simli-api-key"},
]


def _task_definition(secrets):
    return {
        "taskDefinition": {
            "containerDefinitions": [
                {
                    "environment": [
                        {"name": "GRAPHQL_ENDPOINT", "value": "https://example.invalid/graphql"},
                        {"name": "MEETING_ID", "value": ""},
                    ],
                    "secrets": secrets,
                }
            ]
        }
    }


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch, module, task_definition, secret_values=None, error=None
):
    """Point the launcher's boto3 at an ECS stub and a Secrets Manager stub."""
    calls: list[str] = []

    ecs = mock.Mock()
    ecs.describe_task_definition.return_value = task_definition

    secretsmanager = mock.Mock()

    def get_secret_value(SecretId):  # noqa: N803 - boto3's parameter name
        calls.append(SecretId)
        if error is not None:
            raise error
        return {"SecretString": (secret_values or {})[SecretId]}

    secretsmanager.get_secret_value.side_effect = get_secret_value

    def client(service, *args, **kwargs):
        if service == "ecs":
            return ecs
        if service == "secretsmanager":
            return secretsmanager
        raise AssertionError(f"unexpected client: {service}")

    # Patched on the boto3 module the launcher imported, and restored
    # afterwards: the launcher creates its other clients at import time.
    monkeypatch.setattr(module.boto3, "client", client)
    return calls


def test_the_secrets_reach_the_staged_config_under_their_env_var_names(
    module, monkeypatch: pytest.MonkeyPatch
):
    values = {entry["valueFrom"]: f"value-for-{entry['name']}" for entry in SECRET_ENTRIES}
    calls = _patch_clients(
        monkeypatch, module, _task_definition(SECRET_ENTRIES), secret_values=values
    )

    config = module._static_config()  # pylint: disable=protected-access

    assert config["ELEVENLABS_API_KEY"] == "value-for-ELEVENLABS_API_KEY"
    assert config["SIMLI_API_KEY"] == "value-for-SIMLI_API_KEY"
    # The non-secret static configuration still comes through, and a
    # per-meeting field is still left to the launch payload.
    assert config["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"
    assert "MEETING_ID" not in config
    assert sorted(calls) == sorted(values)


def test_the_secrets_are_read_once_per_execution_environment(
    module, monkeypatch: pytest.MonkeyPatch
):
    values = {entry["valueFrom"]: "value" for entry in SECRET_ENTRIES}
    calls = _patch_clients(
        monkeypatch, module, _task_definition(SECRET_ENTRIES), secret_values=values
    )

    for _ in range(4):
        module._static_config()  # pylint: disable=protected-access

    assert len(calls) == len(SECRET_ENTRIES)


def test_no_secret_is_read_when_no_integration_is_configured(
    module, monkeypatch: pytest.MonkeyPatch
):
    calls = _patch_clients(monkeypatch, module, _task_definition([]))

    config = module._static_config()  # pylint: disable=protected-access

    assert calls == []
    assert config["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"


def test_an_unreadable_secret_leaves_the_rest_of_the_config_intact(
    module, monkeypatch: pytest.MonkeyPatch
):
    calls = _patch_clients(
        monkeypatch, module, _task_definition(SECRET_ENTRIES), error=RuntimeError("denied")
    )

    config = module._static_config()  # pylint: disable=protected-access

    assert len(calls) == len(SECRET_ENTRIES)
    assert "ELEVENLABS_API_KEY" not in config
    assert config["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"


def test_the_resolved_credentials_are_redacted_in_the_launchers_logging(module):
    redacted = module._redact(  # pylint: disable=protected-access
        {
            "ELEVENLABS_API_KEY": "0123456789",
            "SIMLI_API_KEY": "0123456789",
            "ZOOM_MEETING_SDK_CLIENT_SECRET": "0123456789",
            "ACS_CONNECTION_STRING": "0123456789",
            "GRAPHQL_ENDPOINT": "https://example.invalid/graphql",
        }
    )
    assert redacted == {
        "ELEVENLABS_API_KEY": "<redacted:10>",
        "SIMLI_API_KEY": "<redacted:10>",
        "ZOOM_MEETING_SDK_CLIENT_SECRET": "<redacted:10>",
        "ACS_CONNECTION_STRING": "<redacted:10>",
        "GRAPHQL_ENDPOINT": "https://example.invalid/graphql",
    }
