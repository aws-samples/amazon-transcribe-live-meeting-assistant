# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The custom resource in lma-main.yaml that empties and deletes the logging and
recordings buckets when the stack is deleted (S3BucketCleanupHandler).

Checked statically (no AWS):

- the handler, its function, role and log group exist whatever
  EnableDataRetentionOnDelete says, and the function is told that setting;
- every resource that uses either bucket is deleted before the handler, because
  it depends on it, directly or through another resource;
- the handler's properties stay the ones existing stacks already have, so a
  stack update does not send it an Update.

The function's inline code is run against a fake S3 client:

- with retention on, a Delete leaves the buckets alone;
- with retention off, a Delete empties and deletes both buckets, recordings first;
- OperationAborted, BucketNotEmpty and other transient errors are retried with
  backoff, other errors fail the request, NoSuchBucket counts as done;
- a failure reason names the objects S3 would not delete, a few at most;
- a retry that would leave too little time to answer CloudFormation fails
  the request instead;
- Create and Update do nothing and keep the physical ID CloudFormation has.
"""

from __future__ import annotations

import inspect
import re
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "lma-main.yaml"

HANDLER = "S3BucketCleanupHandler"
CLEANUP_CHAIN = (
    "S3BucketCleanupHandler",
    "S3BucketCleanupFunction",
    "S3BucketCleanupRole",
    "S3BucketCleanupFunctionLogGroup",
)
BUCKETS = ("LoggingBucket", "RecordingsBucket")

# The nested stacks and custom resource that receive a bucket's name and write
# to it, or change its configuration, while they exist.
DIRECT_DEPENDENTS = (
    "COGNITOSTACK",
    "AISTACK",
    "TRANSCRIPTBEDROCKKB",
    "ASRMICROVMSTACK",
    "WEBSOCKETTRANSCRIBERSTACK",
    "VIRTUALPARTICIPANTSTACK",
    "RecordingsBucketNotification",
)

# Resources that name a bucket but need not outlive the handler: the buckets and
# their policies (CloudFormation deletes them after the handler or finds them
# gone), the cleanup chain itself, and a role whose policy only names the ARN.
MAY_PRECEDE_HANDLER = {
    *BUCKETS,
    "LoggingBucketPolicy",
    "RecordingsBucketPolicy",
    *CLEANUP_CHAIN,
    "RecordingsBucketNotificationFunctionRole",
}


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def resources() -> Dict[str, Any]:
    return yaml.load(MAIN.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]


def _references(node: Any, names: set) -> set:
    """Logical IDs in `names` that `node` names through Ref, GetAtt or Sub."""
    found: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                found.add(value)
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) else str(value).split(".")[0]
                found.add(target)
            elif key == "Fn::Sub":
                text = value[0] if isinstance(value, list) else value
                for name in names:
                    if "${" + name + "}" in text or "${" + name + "." in text:
                        found.add(name)
                found |= _references(
                    value[1] if isinstance(value, list) and len(value) > 1 else None, names
                )
            else:
                found |= _references(value, names)
    elif isinstance(node, list):
        for item in node:
            found |= _references(item, names)
    return found & names


def _depends_on(resource: Dict[str, Any]) -> List[str]:
    value = resource.get("DependsOn") or []
    return [value] if isinstance(value, str) else list(value)


def _dependencies(resources: Dict[str, Any]) -> Dict[str, set]:
    names = set(resources)
    return {
        name: set(_depends_on(resource)) | _references(resource.get("Properties") or {}, names)
        for name, resource in resources.items()
    }


def _closure(start: str, graph: Dict[str, set]) -> set:
    seen: set = set()
    stack = [start]
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# ── template ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", CLEANUP_CHAIN)
def test_the_cleanup_resources_exist_whatever_the_retention_setting(resources, name):
    assert name in resources
    assert "Condition" not in resources[name], (
        f"{name} must be unconditional so DependsOn can name the handler"
    )


def test_the_function_is_told_the_retention_setting(resources):
    variables = resources["S3BucketCleanupFunction"]["Properties"]["Environment"]["Variables"]
    assert variables["RETAIN_DATA_ON_DELETE"] == {"Ref": "EnableDataRetentionOnDelete"}
    assert variables["LOGGING_BUCKET_NAME"] == {"Ref": "LoggingBucket"}


@pytest.mark.parametrize("name", DIRECT_DEPENDENTS)
def test_bucket_users_depend_on_the_cleanup_handler(resources, name):
    assert HANDLER in _depends_on(resources[name]), f"{name} must list {HANDLER} in DependsOn"


def test_every_resource_that_uses_a_bucket_is_deleted_before_the_handler(resources):
    graph = _dependencies(resources)
    users = {
        name
        for name, resource in resources.items()
        if name not in MAY_PRECEDE_HANDLER and _references(resource, set(BUCKETS))
    }
    assert set(DIRECT_DEPENDENTS) <= users, "the check no longer finds the known bucket users"
    missing = sorted(name for name in users if HANDLER not in _closure(name, graph))
    assert not missing, f"resources that use a bucket but do not depend on {HANDLER}: {missing}"


def test_the_handler_does_not_wait_for_a_nested_stack(resources):
    graph = _dependencies(resources)
    stacks = {name for name, r in resources.items() if r["Type"] == "AWS::CloudFormation::Stack"}
    assert not _closure(HANDLER, graph) & stacks


def test_the_handler_properties_are_unchanged_so_updates_do_not_reach_it(resources):
    """Existing stacks have exactly these properties; a new or changed one would
    send the handler an Update."""
    handler = resources[HANDLER]
    assert set(handler["Properties"]) == {"ServiceToken", "StackName"}
    assert handler["Properties"]["ServiceToken"] == {"Fn::GetAtt": "S3BucketCleanupFunction.Arn"}


def test_the_role_grants_the_s3_actions_the_function_uses(resources):
    """ListBucketVersions for the paginator, DeleteObject and DeleteObjectVersion
    for delete_objects, DeleteBucket, and ListBucket for the HeadBucket call
    botocore makes when it has to find a bucket's Region."""
    statements = resources["S3BucketCleanupRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    actions = {action for statement in statements for action in statement["Action"]}
    assert actions == {
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:DeleteBucket",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
    }


def test_the_function_calls_only_the_s3_operations_the_role_grants(resources):
    calls = set(re.findall(r"\bs3\.(\w+)\(", _function_source(resources)))
    assert calls == {"get_paginator", "delete_objects", "delete_bucket"}
    assert "get_paginator('list_object_versions')" in _function_source(resources)


# ── function code ───────────────────────────────────────────────────────────


def _client_error(code: str, operation: str = "DeleteBucket") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakePaginator:
    def __init__(self, s3: "FakeS3") -> None:
        self.s3 = s3

    def paginate(self, Bucket: str):  # noqa: N803
        if Bucket not in self.s3.buckets:
            raise _client_error("NoSuchBucket", "ListObjectVersions")
        versions = [{"Key": k, "VersionId": v} for k, v in self.s3.buckets[Bucket]]
        yield {"Versions": versions[:1], "DeleteMarkers": versions[1:2]}
        yield {"Versions": versions[2:]}


class FakeS3:
    """Buckets hold (key, version) pairs; `delete_errors` queues errors per bucket
    that DeleteBucket raises before it succeeds; `object_errors` maps a bucket's
    keys to the code DeleteObjects reports for them, and those objects stay."""

    def __init__(
        self, buckets: Dict[str, List[tuple]], delete_errors: Optional[Dict[str, List[str]]] = None
    ):
        self.buckets = {name: list(objects) for name, objects in buckets.items()}
        self.delete_errors = {name: list(codes) for name, codes in (delete_errors or {}).items()}
        self.object_errors: Dict[str, Dict[str, str]] = {}
        self.deleted: List[str] = []

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_object_versions"
        return FakePaginator(self)

    def delete_objects(self, Bucket: str, Delete: Dict[str, Any]):  # noqa: N803
        refused = self.object_errors.get(Bucket, {})
        doomed = {(o["Key"], o["VersionId"]) for o in Delete["Objects"] if o["Key"] not in refused}
        self.buckets[Bucket] = [o for o in self.buckets[Bucket] if o not in doomed]
        errors = [
            {"Key": o["Key"], "VersionId": o["VersionId"], "Code": refused[o["Key"]]}
            for o in Delete["Objects"]
            if o["Key"] in refused
        ]
        return {"Errors": errors} if errors else {}

    def delete_bucket(self, Bucket: str):  # noqa: N803
        if Bucket not in self.buckets:
            raise _client_error("NoSuchBucket")
        queued = self.delete_errors.get(Bucket)
        if queued:
            raise _client_error(queued.pop(0))
        if self.buckets[Bucket]:
            raise _client_error("BucketNotEmpty")
        del self.buckets[Bucket]
        self.deleted.append(Bucket)


def _function_source(resources: Dict[str, Any]) -> str:
    return resources["S3BucketCleanupFunction"]["Properties"]["Code"]["ZipFile"]


def _set_defaults(function: Any, **overrides: Any) -> None:
    """Replace the named parameter defaults, keeping the others; a name that is
    not a defaulted positional parameter fails rather than shifting the rest."""
    defaulted = [
        param
        for param in inspect.signature(function).parameters.values()
        if param.default is not inspect.Parameter.empty
    ]
    assert all(param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for param in defaulted)
    unknown = set(overrides) - {param.name for param in defaulted}
    assert not unknown, f"{function.__name__} has no defaulted parameter {sorted(unknown)}"
    function.__defaults__ = tuple(overrides.get(param.name, param.default) for param in defaulted)


@pytest.fixture
def run(resources, monkeypatch):
    """Load the inline function with a fake S3 client and run one request."""

    def _run(
        request_type: str,
        s3: FakeS3,
        retain: str = "false",
        physical_id: Optional[str] = "existing-id",
        recordings: str = "recordings",
        context: Any = None,
        sleeps: Optional[List[float]] = None,
    ):
        responses: List[Dict[str, Any]] = []
        cfnresponse = types.ModuleType("cfnresponse")
        cfnresponse.SUCCESS, cfnresponse.FAILED = "SUCCESS", "FAILED"

        def send(event, context, status, data, physicalResourceId=None, noEcho=False, reason=None):  # noqa: N803
            responses.append(
                {"status": status, "physical_id": physicalResourceId, "reason": reason}
            )

        cfnresponse.send = send
        boto3 = types.ModuleType("boto3")
        boto3.client = lambda service: s3
        monkeypatch.setitem(sys.modules, "cfnresponse", cfnresponse)
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        monkeypatch.setenv("RETAIN_DATA_ON_DELETE", retain)
        monkeypatch.setenv("LOGGING_BUCKET_NAME", "logging")
        monkeypatch.setenv("RECORDINGS_BUCKET_NAME", recordings)

        module = types.ModuleType("s3_bucket_cleanup")
        source = compile(_function_source(resources), "S3BucketCleanupFunction", "exec")
        exec(source, module.__dict__)  # noqa: S102
        sleeps = [] if sleeps is None else sleeps
        # `sleep` defaults to time.sleep, bound when the function was defined.
        _set_defaults(module.delete_bucket_with_retry, sleep=sleeps.append)
        event = {
            "RequestType": request_type,
            "LogicalResourceId": HANDLER,
            "ResourceProperties": {},
        }
        if physical_id:
            event["PhysicalResourceId"] = physical_id
        module.handler(event, context)
        assert len(responses) == 1, "the custom resource must answer CloudFormation exactly once"
        return responses[0], sleeps, module

    return _run


def _two_full_buckets() -> FakeS3:
    return FakeS3({"logging": [("a", "1"), ("a", "2"), ("b", "null")], "recordings": [("r", "1")]})


def test_a_delete_with_retention_on_leaves_the_buckets(run):
    s3 = _two_full_buckets()
    response, _, _ = run("Delete", s3, retain="true")
    assert response["status"] == "SUCCESS"
    assert set(s3.buckets) == {"logging", "recordings"} and not s3.deleted


def test_a_delete_with_retention_off_empties_and_deletes_both_buckets_recordings_first(run):
    s3 = _two_full_buckets()
    response, _, _ = run("Delete", s3)
    assert response["status"] == "SUCCESS"
    assert s3.deleted == ["recordings", "logging"]
    assert response["physical_id"] == "existing-id"


def test_without_a_recordings_bucket_only_the_logging_bucket_is_deleted(run):
    s3 = FakeS3({"logging": [("a", "1")]})
    response, _, _ = run("Delete", s3, recordings="")
    assert response["status"] == "SUCCESS"
    assert s3.deleted == ["logging"]


@pytest.mark.parametrize(
    "code", ["OperationAborted", "BucketNotEmpty", "SlowDown", "InternalError"]
)
def test_transient_errors_are_retried_with_backoff(run, code):
    s3 = _two_full_buckets()
    s3.delete_errors = {"logging": [code, code, code]}
    response, sleeps, _ = run("Delete", s3)
    assert response["status"] == "SUCCESS"
    assert s3.deleted == ["recordings", "logging"]
    assert sleeps == [2, 4, 8]


def test_backoff_is_capped_and_gives_up_after_the_last_attempt(run):
    s3 = _two_full_buckets()
    s3.delete_errors = {"logging": ["OperationAborted"] * 20}
    response, sleeps, module = run("Delete", s3)
    assert response["status"] == "FAILED"
    assert "OperationAborted" in response["reason"]
    assert len(sleeps) == module.MAX_ATTEMPTS - 1
    assert max(sleeps) == module.MAX_DELAY_SECONDS
    assert sum(sleeps) < 600, "the retries must finish well inside the function's 900 s timeout"


class FakeContext:
    """A Lambda context whose remaining time drops by each recorded sleep."""

    def __init__(self, seconds: float, sleeps: List[float]) -> None:
        self.seconds = seconds
        self.sleeps = sleeps

    def get_remaining_time_in_millis(self) -> int:
        return int((self.seconds - sum(self.sleeps)) * 1000)


def test_a_retry_that_would_outlast_the_invocation_fails_while_it_can_still_answer(run):
    s3 = _two_full_buckets()
    s3.delete_errors = {"logging": ["OperationAborted"] * 20}
    sleeps: List[float] = []
    response, _, _ = run("Delete", s3, context=FakeContext(70, sleeps), sleeps=sleeps)
    assert response["status"] == "FAILED"
    assert "OperationAborted" in response["reason"]
    # 70 s at the start: 2, 4, 8 and 16 s each leave the 30 s margin; after them
    # 40 s remain, too few for a 30 s wait plus the margin.
    assert sleeps == [2, 4, 8, 16]


def test_an_error_that_is_not_transient_fails_at_once(run):
    s3 = _two_full_buckets()
    s3.delete_errors = {"recordings": ["AccessDenied"]}
    response, sleeps, _ = run("Delete", s3)
    assert response["status"] == "FAILED"
    assert sleeps == []
    assert "logging" in s3.buckets


def test_a_failure_reason_names_the_objects_that_could_not_be_deleted(run):
    s3 = FakeS3({"logging": [("a", "1")], "recordings": [("r1", "1"), ("r2", "1")]})
    s3.object_errors = {"recordings": {"r1": "AccessDenied", "r2": "AccessDenied"}}
    response, sleeps, module = run("Delete", s3)
    assert response["status"] == "FAILED"
    assert len(sleeps) == module.MAX_ATTEMPTS - 1, "BucketNotEmpty is still retried"
    assert "BucketNotEmpty" in response["reason"]
    assert "r1 (AccessDenied)" in response["reason"]
    assert "r2 (AccessDenied)" in response["reason"]
    assert "logging" in s3.buckets


def test_a_failure_reason_caps_the_objects_it_names(run):
    keys = [f"k{i}" for i in range(10)]
    s3 = FakeS3({"logging": [(key, "1") for key in keys]})
    s3.object_errors = {"logging": {key: "AccessDenied" for key in keys}}
    response, _, module = run("Delete", s3, recordings="")
    assert response["status"] == "FAILED"
    named = [key for key in keys if f"{key} (AccessDenied)" in response["reason"]]
    assert len(named) == module.MAX_REPORTED_FAILURES
    assert f"and {len(keys) - module.MAX_REPORTED_FAILURES} more" in response["reason"]
    assert len(response["reason"]) <= 1024


def test_set_defaults_rejects_an_unknown_parameter():
    def function(a, b=1, c=2):
        return a, b, c

    with pytest.raises(AssertionError, match="no defaulted parameter"):
        _set_defaults(function, d=3)
    _set_defaults(function, c=5)
    assert function(0) == (0, 1, 5)


def test_a_bucket_that_is_already_gone_counts_as_deleted(run):
    s3 = FakeS3({"logging": [("a", "1")]})  # no recordings bucket
    response, _, _ = run("Delete", s3)
    assert response["status"] == "SUCCESS"
    assert s3.deleted == ["logging"]


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_create_and_update_do_nothing(run, request_type):
    s3 = _two_full_buckets()
    physical_id = None if request_type == "Create" else "existing-id"
    response, _, _ = run(request_type, s3, physical_id=physical_id)
    assert response["status"] == "SUCCESS"
    assert not s3.deleted
    # A changed physical ID on Update would make CloudFormation delete the old one.
    expected = f"S3BucketCleanup-{HANDLER}" if request_type == "Create" else "existing-id"
    assert response["physical_id"] == expected
