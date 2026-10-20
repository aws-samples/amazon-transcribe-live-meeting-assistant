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


def version(number: str, created, state: str = "SUCCESSFUL") -> dict:
    return {"imageVersion": number, "createdAt": created, "state": state}


def fake_client(
    versions: list[dict], active: str | None, *, blocked=(), code="ConflictException"
) -> mock.MagicMock:
    """A client whose deletes refuse the given versions once, then succeed."""
    client = mock.MagicMock()
    client.get_microvm_image.return_value = {"latestActiveImageVersion": active}
    client.list_microvm_image_versions.return_value = {"items": versions}
    refused = set(blocked)

    def delete(_arn, number):
        if number in refused:
            refused.discard(number)
            raise MicrovmErrorStub("still in use", code=code, status=409)
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
    versions = [version(str(n), 1700000000 + n) for n in range(1, 11)]
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
        version("3", 1700000003),
        version("1", 1700000001),
        version("2", 1700000002),
    ]
    client = fake_client(versions, active="3")
    module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    removed = [call[0][1] for call in client.delete_microvm_image_version.call_args_list]
    assert removed == ["1", "2"]


def test_retries_a_version_that_another_still_depends_on(module) -> None:
    versions = [version(str(n), 1700000000 + n) for n in range(1, 4)]
    client = fake_client(versions, active="3", blocked={"2"})
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["Deleted"] == 2, "a refused delete must be attempted again"


def test_retries_after_a_pass_that_removed_nothing_at_all(module) -> None:
    """The refusals are transient, so an unproductive pass is not the end.

    Every surplus version refusing on its first attempt is the exact scenario
    the retry exists for -- the base-image chain is still settling. Stopping at
    the first pass that achieved nothing would cancel the retry precisely when
    it is needed, and the whole run would delete nothing while still reporting
    success.
    """
    versions = [version(str(n), 1700000000 + n) for n in range(1, 5)]
    client = fake_client(versions, active="9", blocked={"1", "2", "3", "4"})
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["Deleted"] == 4


def test_gives_up_on_a_version_that_never_becomes_removable(module) -> None:
    """Bounded, so a permanently blocked version cannot run out the clock."""
    client = mock.MagicMock()
    client.get_microvm_image.return_value = {"latestActiveImageVersion": "9"}
    client.list_microvm_image_versions.return_value = {
        "items": [version("1", 1700000000)]
    }
    client.delete_microvm_image_version.side_effect = MicrovmErrorStub(
        "still in use", code="ConflictException", status=409
    )
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["Deleted"] == 0
    assert client.delete_microvm_image_version.call_count == module.MAX_PASSES


def test_retries_a_refusal_identified_only_by_its_status(module) -> None:
    """The error type is not always in the body, so the status has to carry it.

    A rest-json service puts the type in a response header, and a client that
    reads only the body leaves the code empty. The retry therefore cannot rest
    on the code alone.
    """
    versions = [version(str(n), 1700000000 + n) for n in range(1, 4)]
    client = fake_client(versions, active="9", blocked={"2"}, code="")
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["Deleted"] == 3


def test_stops_while_there_is_still_time_to_report(module) -> None:
    """A custom resource that never answers hangs the stack for an hour.

    So the prune gives up its remaining work rather than its chance to reply.
    """
    versions = [version(str(n), 1700000000 + n) for n in range(1, 21)]
    client = fake_client(versions, active="99")
    context = mock.MagicMock()
    # Plenty of time for two deletes, then under the floor.
    context.get_remaining_time_in_millis.side_effect = [90_000, 90_000] + [5_000] * 60
    outcome = module.prune_microvm_image_versions(
        client, IMAGE_ARN, keep=0, context=context
    )
    assert outcome["Deleted"] == 2
    assert client.delete_microvm_image_version.call_count == 2


def test_runs_to_completion_when_the_context_reports_no_deadline(module) -> None:
    versions = [version(str(n), 1700000000 + n) for n in range(1, 6)]
    client = fake_client(versions, active="9")
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0, context=None)
    assert outcome["Deleted"] == 5


def test_leaves_versions_already_being_deleted_alone(module) -> None:
    versions = [
        version("1", 1700000001, state="DELETED"),
        version("2", 1700000002, state="DELETING"),
        version("3", 1700000003),
    ]
    client = fake_client(versions, active="3")
    module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert client.delete_microvm_image_version.call_count == 0


def test_follows_pagination(module) -> None:
    client = mock.MagicMock()
    client.get_microvm_image.return_value = {"latestActiveImageVersion": "3"}
    client.list_microvm_image_versions.side_effect = [
        {"items": [version("1", 1700000001)], "nextToken": "more"},
        {"items": [version("2", 1700000002)]},
    ]
    client.delete_microvm_image_version.return_value = {}
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["TotalVersions"] == 2
    assert outcome["Deleted"] == 2


def test_asks_for_a_page_size_the_service_accepts(module) -> None:
    """ListMicrovmImageVersions rejects maxResults above 50 outright.

    It does not clamp, so asking for more fails the call, the broad handler
    reports success, and nothing is ever pruned in any deployment -- a failure
    that looks exactly like working.
    """
    assert 1 <= module.MAX_RESULTS_PER_PAGE <= 50
    client = fake_client([version("1", 1700000001)], active="1")
    module.prune_microvm_image_versions(client, IMAGE_ARN, keep=5)
    assert client.list_microvm_image_versions.call_args.kwargs["max_results"] <= 50


def test_stops_paging_when_the_token_stops_changing(module) -> None:
    """The service repeats a trailing token with an empty final page."""
    client = mock.MagicMock()
    client.get_microvm_image.return_value = {"latestActiveImageVersion": "1"}
    client.list_microvm_image_versions.return_value = {
        "items": [version("1", 1700000001)],
        "nextToken": "same-token-for-ever",
    }
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=5)
    assert client.list_microvm_image_versions.call_count <= module.MAX_PAGES
    assert outcome["TotalVersions"] >= 1


def test_orders_by_a_numeric_timestamp(module) -> None:
    """createdAt is a timestamp, which this client sees as a number.

    An earlier version sorted it as text with an empty-string fallback, which
    raises on a number/string comparison rather than degrading.
    """
    versions = [
        version("12", 1700000300),
        version("3", 1700000100),
        version("7", 1700000200),
    ]
    client = fake_client(versions, active="12")
    module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    removed = [call[0][1] for call in client.delete_microvm_image_version.call_args_list]
    assert removed == ["3", "7"]


def test_tolerates_a_version_with_no_timestamp(module) -> None:
    """Mixed shapes must not raise; the version number is the tiebreak."""
    versions = [
        {"imageVersion": "2", "state": "SUCCESSFUL"},
        version("3", 1700000100),
        version("4", 1700000200),
    ]
    client = fake_client(versions, active="4")
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    assert outcome["Deleted"] == 2


def test_always_leaves_the_newest_version_behind(module) -> None:
    """An image with no versions cannot be launched at all.

    So the newest is retained whatever the keep count says and whether or not
    the image reported an active version.
    """
    versions = [version(str(n), 1700000000 + n) for n in range(1, 4)]
    client = mock.MagicMock()
    client.get_microvm_image.return_value = {}
    client.list_microvm_image_versions.return_value = {"items": versions}
    client.delete_microvm_image_version.return_value = {}
    outcome = module.prune_microvm_image_versions(client, IMAGE_ARN, keep=0)
    removed = [call[0][1] for call in client.delete_microvm_image_version.call_args_list]
    assert "3" not in removed, "the newest version must survive"
    assert outcome["Remaining"] >= 1


def test_removes_nothing_when_the_image_is_under_the_keep_count(module) -> None:
    versions = [version(str(n), 1700000000 + n) for n in range(1, 4)]
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
