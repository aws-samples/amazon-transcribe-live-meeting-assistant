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

The properties asserted here:

* The values reach the staged configuration under the names the task
  definition gave them.
* **Every launch reads the secrets afresh.** The task definition is read once
  per execution environment, because a registered revision is immutable, but
  the secret versions it points at change whenever an operator rotates a
  credential and without any stack update. The documented rotation procedure
  for all four Virtual Participant credentials says the next meeting to start
  uses the new value, which only holds if the resolution happens per launch.
* A deployment that configured no integration makes no Secrets Manager call.
* One unreadable secret costs that one feature rather than the whole meeting,
  and does not cost the other ~60 static configuration values either.
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

    Fresh per test because the launcher caches the task definition for the life
    of the execution environment, which is part of what is under test. Loaded
    by path for the reason test_image_version_pruner.py gives: several
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


class _Harness:
    """The launcher's AWS calls, stubbed, plus what each launch staged.

    `describes` and `secret_reads` count the two API calls whose frequency is
    the point of these tests. `staged` holds the configuration each launch
    wrote to the task registry.
    """

    def __init__(self):
        self.describes = 0
        self.secret_reads: list[str] = []
        self.staged: list[dict] = []
        self.secret_values: dict[str, str] = {}
        self.task_definition: dict = {}
        self.describe_error: Exception | None = None
        self.secret_error: Exception | None = None


def _patch(
    monkeypatch: pytest.MonkeyPatch, module, secrets, secret_values=None, secret_error=None
) -> _Harness:
    """Stub the launcher's ECS, Secrets Manager and registry write."""
    harness = _Harness()
    harness.task_definition = _task_definition(secrets)
    harness.secret_values = dict(secret_values or {})
    harness.secret_error = secret_error

    ecs = mock.Mock()

    def describe_task_definition(taskDefinition):  # noqa: N803 - boto3's parameter name
        harness.describes += 1
        if harness.describe_error is not None:
            raise harness.describe_error
        return harness.task_definition

    ecs.describe_task_definition.side_effect = describe_task_definition

    secretsmanager = mock.Mock()

    def get_secret_value(SecretId):  # noqa: N803 - boto3's parameter name
        harness.secret_reads.append(SecretId)
        if harness.secret_error is not None:
            raise harness.secret_error
        return {"SecretString": harness.secret_values[SecretId]}

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

    # Capture the staged configuration and report the write as failed, which
    # returns the handler before it reaches RunMicrovm. This is how these tests
    # observe the real composition in lambda_handler rather than reimplementing
    # it.
    def write(vp_id, config):
        harness.staged.append(config)
        return False

    monkeypatch.setattr(module, "_write_config_to_registry", write)
    return harness


def _launch(module, vp_id="vp-1"):
    """One launch through the real handler, stopping at the registry write."""
    result = module.lambda_handler(
        {"data": {"virtualParticipantId": vp_id, "meetingID": "123 456 789"}}, None
    )
    assert result["ok"] is False  # the stubbed registry write, not a real failure
    return result


def test_the_secrets_reach_the_staged_config_under_their_env_var_names(
    module, monkeypatch: pytest.MonkeyPatch
):
    values = {entry["valueFrom"]: f"value-for-{entry['name']}" for entry in SECRET_ENTRIES}
    harness = _patch(monkeypatch, module, SECRET_ENTRIES, secret_values=values)

    _launch(module)

    config = harness.staged[0]
    assert config["ELEVENLABS_API_KEY"] == "value-for-ELEVENLABS_API_KEY"
    assert config["SIMLI_API_KEY"] == "value-for-SIMLI_API_KEY"
    # The non-secret static configuration still comes through, and the
    # per-meeting fields still come from the launch payload.
    assert config["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"
    assert config["MEETING_ID"] == "123 456 789"
    assert sorted(harness.secret_reads) == sorted(values)


def test_a_rotated_secret_reaches_the_next_meeting(
    module, monkeypatch: pytest.MonkeyPatch
):
    """The property the documented rotation procedure depends on.

    A secret's current version changes with no stack update, so a launcher
    instance that resolved the credentials once would go on handing every
    later meeting the value it read first. Rotation is documented as taking
    effect for the next meeting to start, so each launch must read again.
    """
    arn = SECRET_ENTRIES[0]["valueFrom"]
    values = {entry["valueFrom"]: "before-rotation" for entry in SECRET_ENTRIES}
    harness = _patch(monkeypatch, module, SECRET_ENTRIES, secret_values=values)

    _launch(module, vp_id="vp-before")
    assert harness.staged[0]["ELEVENLABS_API_KEY"] == "before-rotation"

    harness.secret_values[arn] = "after-rotation"
    _launch(module, vp_id="vp-after")

    assert harness.staged[1]["ELEVENLABS_API_KEY"] == "after-rotation"
    assert len(harness.secret_reads) == 2 * len(SECRET_ENTRIES)
    # The task definition itself is immutable once registered, so it is still
    # read only once however many meetings this instance handles.
    assert harness.describes == 1


def test_no_secret_is_read_when_no_integration_is_configured(
    module, monkeypatch: pytest.MonkeyPatch
):
    harness = _patch(monkeypatch, module, [])

    _launch(module)

    assert harness.secret_reads == []
    assert harness.staged[0]["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"


def test_an_unreadable_secret_leaves_the_rest_of_the_config_intact(
    module, monkeypatch: pytest.MonkeyPatch
):
    harness = _patch(
        monkeypatch, module, SECRET_ENTRIES, secret_error=RuntimeError("denied")
    )

    _launch(module)

    assert len(harness.secret_reads) == len(SECRET_ENTRIES)
    assert "ELEVENLABS_API_KEY" not in harness.staged[0]
    assert harness.staged[0]["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"


def test_an_unreadable_secret_does_not_cost_later_meetings_their_static_config(
    module, monkeypatch: pytest.MonkeyPatch
):
    """A failed secret read is confined to the launch that made it.

    The task definition and the secret values are resolved separately, so a
    credential that cannot be read on one launch neither persists as an empty
    result nor takes the ~60 static configuration values down with it.
    """
    values = {entry["valueFrom"]: "a-value" for entry in SECRET_ENTRIES}
    harness = _patch(
        monkeypatch,
        module,
        SECRET_ENTRIES,
        secret_values=values,
        secret_error=RuntimeError("throttled"),
    )

    _launch(module, vp_id="vp-throttled")
    assert "ELEVENLABS_API_KEY" not in harness.staged[0]
    assert harness.staged[0]["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"

    harness.secret_error = None
    _launch(module, vp_id="vp-recovered")

    assert harness.staged[1]["ELEVENLABS_API_KEY"] == "a-value"
    assert harness.staged[1]["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"


def test_an_unreadable_task_definition_is_retried_on_the_next_meeting(
    module, monkeypatch: pytest.MonkeyPatch
):
    """The same confinement for the other of the two reads."""
    values = {entry["valueFrom"]: "a-value" for entry in SECRET_ENTRIES}
    harness = _patch(monkeypatch, module, SECRET_ENTRIES, secret_values=values)
    harness.describe_error = RuntimeError("throttled")

    _launch(module, vp_id="vp-throttled")
    assert "GRAPHQL_ENDPOINT" not in harness.staged[0]

    harness.describe_error = None
    _launch(module, vp_id="vp-recovered")

    assert harness.staged[1]["GRAPHQL_ENDPOINT"] == "https://example.invalid/graphql"
    assert harness.staged[1]["ELEVENLABS_API_KEY"] == "a-value"
    assert harness.describes == 2


def test_redact_covers_the_integration_credentials(module):
    """`_redact` is exercised directly, not through a launch.

    The launcher applies `_redact` only to the per-meeting config, which can
    hold nothing but FIELD_MAP keys, so no production path reaches it with
    these four names. They are in SECRET_KEYS so that redaction already covers
    them if `_redact` is ever applied to the merged configuration, which is
    where they live; this asserts that it does.
    """
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
