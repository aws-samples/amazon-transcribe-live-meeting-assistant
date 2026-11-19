# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the MicroVM launcher's RunMicrovm client token.

RunMicrovm is idempotent on its client token. A first launch uses the Virtual
Participant's id, so a retried invocation of it cannot start a second MicroVM.
A rejoin is a further launch of the same Virtual Participant and passes a
`launchToken` of its own (docs/virtual-participant.md#rejoin).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1] / "lambda_functions" / "microvm_launcher"


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
        yield _load("microvm_launcher_token_under_test", LAUNCHER_DIR / "index.py")
    finally:
        sys.path.remove(str(LAUNCHER_DIR))
        sys.modules.pop("microvm_launcher_token_under_test", None)
        if previous is None:
            sys.modules.pop("microvm_client", None)
        else:
            sys.modules["microvm_client"] = previous


def _client_token_sent(module, monkeypatch: pytest.MonkeyPatch, data: dict) -> str:
    """The client token one launch passes to RunMicrovm (which is made to fail)."""
    monkeypatch.setattr(module, "_container_definition", lambda: {})
    monkeypatch.setattr(module, "_static_config", lambda container: {})
    monkeypatch.setattr(module, "_integration_secrets", lambda container: {})
    staged = {}
    monkeypatch.setattr(
        module, "_write_config_to_registry", lambda vp_id, config: staged.update(config) or True
    )
    client = mock.Mock()
    client.run_microvm.side_effect = module.MicrovmError("stop here")
    monkeypatch.setattr(module, "microvms", client)

    module.lambda_handler({"apiInfo": {"httpMethod": "POST"}, "data": data}, None)

    assert "launchToken" not in staged and "LAUNCH_TOKEN" not in staged, (
        "the token is not passed to the container"
    )
    return client.run_microvm.call_args.kwargs["clientToken"]


def test_a_first_launch_is_idempotent_on_the_virtual_participants_id(module, monkeypatch):
    assert _client_token_sent(module, monkeypatch, {"virtualParticipantId": "vp-1"}) == "vp-1"


def test_a_rejoin_launches_with_its_own_token(module, monkeypatch):
    token = _client_token_sent(
        module, monkeypatch, {"virtualParticipantId": "vp-1", "launchToken": "vp-1-s2"}
    )
    assert token == "vp-1-s2"


@pytest.mark.parametrize("blank", ["", "   ", None, 7])
def test_a_blank_launch_token_falls_back_to_the_id(module, monkeypatch, blank):
    token = _client_token_sent(
        module, monkeypatch, {"virtualParticipantId": "vp-1", "launchToken": blank}
    )
    assert token == "vp-1"
