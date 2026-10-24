# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for where the MicroVM launcher sends a Virtual Participant's logs.

The image's `Logging` setting covers only its build, snapshot and validate
output. A running MicroVM writes its own output to the group named in the
RunMicrovm call that started it, and to a service-created group named after the
image when the call names none. So the launcher passes the stack's declared
group, from `MICROVM_LOG_GROUP_NAME`, on every launch.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

LAUNCHER_DIR = (
    Path(__file__).resolve().parents[1] / "lambda_functions" / "microvm_launcher"
)
LOG_GROUP = "/aws/lambda-microvms/LMA-Test-vp-logs"


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
        yield _load("microvm_launcher_logging_under_test", LAUNCHER_DIR / "index.py")
    finally:
        sys.path.remove(str(LAUNCHER_DIR))
        sys.modules.pop("microvm_launcher_logging_under_test", None)
        if previous is None:
            sys.modules.pop("microvm_client", None)
        else:
            sys.modules["microvm_client"] = previous


def _run_microvm_kwargs(module, monkeypatch: pytest.MonkeyPatch) -> dict:
    """The arguments one launch passes to RunMicrovm.

    Everything before the call is stubbed, and RunMicrovm itself fails, so the
    handler returns straight after it without polling or writing the registry.
    """
    monkeypatch.setattr(module, "_container_definition", lambda: {})
    monkeypatch.setattr(module, "_static_config", lambda container: {})
    monkeypatch.setattr(module, "_integration_secrets", lambda container: {})
    monkeypatch.setattr(module, "_write_config_to_registry", lambda vp_id, config: True)
    client = mock.Mock()
    client.run_microvm.side_effect = module.MicrovmError("stop here")
    monkeypatch.setattr(module, "microvms", client)

    result = module.lambda_handler({"data": {"virtualParticipantId": "vp-1"}}, None)

    assert result["ok"] is False
    client.run_microvm.assert_called_once()
    return client.run_microvm.call_args.kwargs


def test_every_launch_names_the_stacks_log_group(module, monkeypatch) -> None:
    monkeypatch.setenv("MICROVM_LOG_GROUP_NAME", LOG_GROUP)

    kwargs = _run_microvm_kwargs(module, monkeypatch)

    assert kwargs["logging"] == {"cloudWatch": {"logGroup": LOG_GROUP}}


def test_an_unset_log_group_still_launches_with_the_service_default(
    module, monkeypatch
) -> None:
    """A missing variable costs log placement, not the meeting: the member is
    omitted (the client drops None values) rather than sent empty."""
    monkeypatch.delenv("MICROVM_LOG_GROUP_NAME", raising=False)

    kwargs = _run_microvm_kwargs(module, monkeypatch)

    assert kwargs["logging"] is None


def test_the_client_sends_the_logging_member_in_the_request_body(module) -> None:
    """The vendored client builds the body itself, so check the member survives
    serialisation in the shape the service model defines: a union whose
    `cloudWatch` arm carries `logGroup`."""
    client = sys.modules["microvm_client"].MicrovmClient(
        region="us-west-2", session=mock.MagicMock()
    )
    with mock.patch.object(client, "_call", return_value={"microvmId": "m"}) as called:
        client.run_microvm(
            imageIdentifier="img",
            logging={"cloudWatch": {"logGroup": LOG_GROUP}},
        )
    _, _, body = called.call_args[0]
    assert json.loads(json.dumps(body))["logging"] == {"cloudWatch": {"logGroup": LOG_GROUP}}
