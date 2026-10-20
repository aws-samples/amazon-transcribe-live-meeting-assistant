# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the MicroVM image version pruner (no AWS calls).

The two properties worth guarding are both about not breaking a deployment:
the active version is never removed, and nothing the pruner runs into reports
failure to CloudFormation. The third is about the API's own awkwardness --
versions chain to the one they were built on, so deletes have to go oldest
first and tolerate being refused.
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


@pytest.fixture(scope="module")
def module():
    # The pruner imports microvm_client, which ships beside it in the same
    # deployment package, so put that directory on the path first.
    sys.path.insert(0, str(LAUNCHER_DIR))
    spec = importlib.util.spec_from_file_location(
        "image_version_pruner", LAUNCHER_DIR / "image_version_pruner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["image_version_pruner"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(LAUNCHER_DIR))


IMAGE_ARN = "arn:aws:lambda:us-west-2:123456789012:microvm-image:lma-vp"


def version(number: str, created: str, state: str = "SUCCESSFUL") -> dict:
    return {"imageVersion": number, "createdAt": created, "state": state}


def fake_client(versions: list[dict], active: str | None, *, blocked=()) -> mock.MagicMock:
    """A client whose deletes refuse the given versions once, then succeed."""
    client = mock.MagicMock()
    client.get_microvm_image.return_value = {"latestActiveImageVersion": active}
    client.list_microvm_image_versions.return_value = {"items": versions}
    refused = set(blocked)

    def delete(_arn, number):
        if number in refused:
            refused.discard(number)
            raise MicrovmErrorStub("still in use", code="ConflictException", status=409)
        return {"imageVersion": number, "state": "DELETING"}

    client.delete_microvm_image_version.side_effect = delete
    return client


class MicrovmErrorStub(RuntimeError):
    def __init__(self, message, *, status=None, code=""):
        super().__init__(message)
        self.status = status
        self.code = code


@pytest.fixture(autouse=True)
def _error_type_matches(module):
    """The pruner catches the client's own error type, so the stub must be it."""
    with mock.patch.object(module, "MicrovmError", MicrovmErrorStub):
        yield


@pytest.fixture(autouse=True)
def _no_sleeping(module):
    with mock.patch.object(module, "PASS_PAUSE_SECONDS", 0):
        yield


def test_keeps_the_active_version_and_the_requested_number_of_recent_ones(module) -> None:
    versions = [version(str(n), f"2026-10-{n:02d}T00:00:00Z") for n in range(1, 11)]
    client = fake_client(versions, active="10")
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=3)

    removed = [call[0][1] for call in client.delete_microvm_image_version.call_args_list]
    # 10 versions, the active one plus 3 recent kept, so 6 go.
    assert outcome["Deleted"] == 6
    assert "10" not in removed, "the active version must never be removed"
    assert set(removed) == {"1", "2", "3", "4", "5", "6"}


def test_removes_oldest_first(module) -> None:
    """The only order the base-image chain permits."""
    versions = [
        version("3", "2026-10-03T00:00:00Z"),
        version("1", "2026-10-01T00:00:00Z"),
        version("2", "2026-10-02T00:00:00Z"),
    ]
    client = fake_client(versions, active="3")
    module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    removed = [call[0][1] for call in client.delete_microvm_image_version.call_args_list]
    assert removed == ["1", "2"]


def test_retries_a_version_that_another_still_depends_on(module) -> None:
    versions = [version(str(n), f"2026-10-0{n}T00:00:00Z") for n in range(1, 4)]
    client = fake_client(versions, active="3", blocked={"2"})
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["Deleted"] == 2, "a refused delete must be attempted again"


def test_stops_when_a_whole_pass_removes_nothing(module) -> None:
    """Otherwise a permanently blocked version burns the function's whole timeout."""
    client = mock.MagicMock()
    client.get_microvm_image.return_value = {"latestActiveImageVersion": "9"}
    client.list_microvm_image_versions.return_value = {
        "items": [version("1", "2026-10-01T00:00:00Z")]
    }
    client.delete_microvm_image_version.side_effect = MicrovmErrorStub(
        "still in use", code="ConflictException", status=409
    )
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["Deleted"] == 0
    # One attempt, then the pass that achieved nothing ends it.
    assert client.delete_microvm_image_version.call_count == 1


def test_leaves_versions_already_being_deleted_alone(module) -> None:
    versions = [
        version("1", "2026-10-01T00:00:00Z", state="DELETED"),
        version("2", "2026-10-02T00:00:00Z", state="DELETING"),
        version("3", "2026-10-03T00:00:00Z"),
    ]
    client = fake_client(versions, active="3")
    module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert client.delete_microvm_image_version.call_count == 0


def test_follows_pagination(module) -> None:
    client = mock.MagicMock()
    client.get_microvm_image.return_value = {"latestActiveImageVersion": "3"}
    client.list_microvm_image_versions.side_effect = [
        {"items": [version("1", "2026-10-01T00:00:00Z")], "nextToken": "more"},
        {"items": [version("2", "2026-10-02T00:00:00Z")]},
    ]
    client.delete_microvm_image_version.return_value = {}
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["TotalVersions"] == 2
    assert outcome["Deleted"] == 2


def test_removes_nothing_when_the_image_is_under_the_keep_count(module) -> None:
    versions = [version(str(n), f"2026-10-0{n}T00:00:00Z") for n in range(1, 4)]
    client = fake_client(versions, active="3")
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=5)
    assert outcome["Deleted"] == 0
    assert client.delete_microvm_image_version.call_count == 0


# -- the CloudFormation contract --------------------------------------------


def _sent_response(urlopen) -> dict:
    request = urlopen.call_args[0][0]
    return json.loads(request.data.decode())


def _event(request_type="Create", **properties) -> dict:
    return {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource/response",
        "StackId": "stack-1",
        "RequestId": "request-1",
        "LogicalResourceId": "VPMicrovmImagePruner",
        "ResourceProperties": {"ImageArn": IMAGE_ARN, **properties},
    }


@pytest.fixture
def urlopen():
    response = mock.MagicMock()
    response.status = 200
    response.__enter__ = lambda self: self
    response.__exit__ = lambda *a: None
    with mock.patch("urllib.request.urlopen", return_value=response) as patched:
        yield patched


def test_reports_success_after_pruning(module, urlopen) -> None:
    with mock.patch.object(module, "MicrovmClient"), mock.patch.object(
        module, "prune_microvm_image_versions",
        return_value={"TotalVersions": 9, "ActiveVersion": "9", "Deleted": 4, "Remaining": 5},
    ):
        module.lambda_handler(_event(), mock.MagicMock())
    body = _sent_response(urlopen)
    assert body["Status"] == "SUCCESS"
    assert "Removed 4 of 9" in body["Reason"]


def test_reports_success_when_pruning_could_not_run(module, urlopen) -> None:
    """Housekeeping does not get to block a deployment."""
    with mock.patch.object(module, "MicrovmClient"), mock.patch.object(
        module, "prune_microvm_image_versions", side_effect=RuntimeError("API said no")
    ):
        module.lambda_handler(_event(), mock.MagicMock())
    body = _sent_response(urlopen)
    assert body["Status"] == "SUCCESS"
    assert "API said no" in body["Reason"]


def test_reports_success_without_an_image_arn(module, urlopen) -> None:
    event = _event()
    del event["ResourceProperties"]["ImageArn"]
    module.lambda_handler(event, mock.MagicMock())
    assert _sent_response(urlopen)["Status"] == "SUCCESS"


def test_prunes_nothing_on_stack_delete(module, urlopen) -> None:
    """The image goes with the stack, and its versions go with the image."""
    with mock.patch.object(module, "prune_microvm_image_versions") as pruned:
        module.lambda_handler(_event("Delete"), mock.MagicMock())
    pruned.assert_not_called()
    assert _sent_response(urlopen)["Status"] == "SUCCESS"


def test_honours_the_keep_count_from_the_template(module, urlopen) -> None:
    with mock.patch.object(module, "MicrovmClient"), mock.patch.object(
        module, "prune_microvm_image_versions",
        return_value={"TotalVersions": 1, "ActiveVersion": "1", "Deleted": 0, "Remaining": 1},
    ) as pruned:
        module.lambda_handler(_event(KeepVersions="2"), mock.MagicMock())
    assert pruned.call_args[0][2] == 2


def test_defaults_the_keep_count_when_the_template_omits_it(module, urlopen) -> None:
    with mock.patch.object(module, "MicrovmClient"), mock.patch.object(
        module, "prune_microvm_image_versions",
        return_value={"TotalVersions": 1, "ActiveVersion": "1", "Deleted": 0, "Remaining": 1},
    ) as pruned:
        module.lambda_handler(_event(), mock.MagicMock())
    assert pruned.call_args[0][2] == module.DEFAULT_KEEP_VERSIONS


def test_response_omits_the_signed_url_from_its_logs(module, urlopen, caplog) -> None:
    """The ResponseURL is a presigned S3 URL; it does not belong in CloudWatch."""
    import logging

    caplog.set_level(logging.INFO)
    with mock.patch.object(module, "MicrovmClient"), mock.patch.object(
        module, "prune_microvm_image_versions",
        return_value={"TotalVersions": 1, "ActiveVersion": "1", "Deleted": 0, "Remaining": 1},
    ):
        module.lambda_handler(_event(), mock.MagicMock())
    assert "cloudformation-custom-resource/response" not in caplog.text


def test_both_stacks_ship_the_same_pruner_and_client() -> None:
    """The VP and ASR stacks each package their own copy of these two files.

    Each stack's Lambda deployment package is zipped from its own directory, so
    the files are duplicated rather than shared. Editing one copy and not the
    other is the obvious way for that to go wrong, and only this test would
    notice: the ASR stack's tests do not exercise the pruner.
    """
    asr_dir = (
        Path(__file__).resolve().parents[2]
        / "lma-asr-microvm-stack"
        / "lambda_functions"
        / "asr_launcher"
    )
    for name in ("image_version_pruner.py", "microvm_client.py"):
        assert (LAUNCHER_DIR / name).read_text() == (asr_dir / name).read_text(), (
            f"{name} has drifted between the two stacks"
        )


def test_pruner_has_no_third_party_imports(module) -> None:
    """Same constraint as the client: stdlib plus the client beside it."""
    source = (LAUNCHER_DIR / "image_version_pruner.py").read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    }
    allowed = {"json", "logging", "os", "time", "urllib", "microvm_client", "__future__"}
    assert imports <= allowed, f"unexpected imports: {imports - allowed}"
