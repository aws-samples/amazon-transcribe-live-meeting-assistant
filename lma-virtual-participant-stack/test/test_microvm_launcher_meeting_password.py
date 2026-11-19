# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The MicroVM launcher stages no meeting password.

The Virtual Participant reads its meeting password from its own record at
startup (backend/src/startup-meeting-password.ts), so neither the configuration
the launcher stages in the task registry nor the RunMicrovm request carries one,
whatever the launch event holds.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1] / "lambda_functions" / "microvm_launcher"
PASSWORD = "Pw-In-The-Event-42"


def _load(name: str, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch):
    """A fresh launcher module, loaded by path with its sibling `microvm_client`."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("MICROVM_IMAGE_ARN", "arn:aws:lambda:us-west-2:123456789012:microvm-image/vp")
    monkeypatch.setenv("EXECUTION_ROLE_ARN", "arn:aws:iam::123456789012:role/vp-task")
    previous = sys.modules.get("microvm_client")
    sys.path.insert(0, str(LAUNCHER_DIR))
    try:
        _load("microvm_client", LAUNCHER_DIR / "microvm_client.py")
        yield _load("microvm_launcher_password_under_test", LAUNCHER_DIR / "index.py")
    finally:
        sys.path.remove(str(LAUNCHER_DIR))
        sys.modules.pop("microvm_launcher_password_under_test", None)
        if previous is None:
            sys.modules.pop("microvm_client", None)
        else:
            sys.modules["microvm_client"] = previous


def _launch(module, monkeypatch: pytest.MonkeyPatch, event: dict) -> tuple[dict, dict]:
    """What one launch stages in the registry, and the RunMicrovm request."""
    monkeypatch.setattr(module, "_container_definition", lambda: {})
    monkeypatch.setattr(module, "_static_config", lambda container: {})
    monkeypatch.setattr(module, "_integration_secrets", lambda container: {})
    staged: dict = {}
    monkeypatch.setattr(
        module, "_write_config_to_registry", lambda vp_id, config: staged.update(config) or True
    )
    client = mock.Mock()
    client.run_microvm.side_effect = module.MicrovmError("stop here")
    monkeypatch.setattr(module, "microvms", client)

    module.lambda_handler(event, None)
    return staged, client.run_microvm.call_args.kwargs


@pytest.mark.parametrize(
    "event",
    [
        # The state machine's shape.
        {
            "apiInfo": {"httpMethod": "POST"},
            "data": {
                "virtualParticipantId": "vp-1",
                "meetingPlatform": "ZOOM",
                "meetingID": "123456789",
                "meetingPassword": PASSWORD,
            },
        },
        # The flat shape of an EventBridge schedule created by an earlier release.
        {
            "virtualParticipantId": "vp-1",
            "meetingPlatform": "ZOOM",
            "meetingId": "123456789",
            "meetingPassword": PASSWORD,
        },
    ],
)
def test_launcher_stages_no_meeting_password(module, monkeypatch, event) -> None:
    staged, request = _launch(module, monkeypatch, event)

    assert staged["VIRTUAL_PARTICIPANT_ID"] == "vp-1"
    assert staged["MEETING_ID"] == "123456789"
    assert "MEETING_PASSWORD" not in staged
    assert PASSWORD not in json.dumps(staged)
    assert PASSWORD not in json.dumps(request, default=str)


def test_launcher_field_map_has_no_meeting_password(module) -> None:
    assert "MEETING_PASSWORD" not in module.FIELD_MAP
    candidates = {name for names in module.FIELD_MAP.values() for name in names}
    assert "meetingPassword" not in candidates
