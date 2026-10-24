# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The troubleshooting agent's IAM role, asserted from the template (no AWS).

Two kinds of assertion here, and they pull in opposite directions, which is
why they are tested together:

* **Narrow enough.** The role is scoped to one deployment and changes nothing
  it diagnoses, and the grants that could widen it are named individually so a
  later edit has to change a test to loosen one.
* **Wide enough to actually work.** Several actions support no resource-level
  permissions, and scoping those denies every call. The unit tests for the
  tools stub the Logs client, so nothing else in this repository would catch
  that — a diagnosis would simply fail on a deployed stack with every log group
  reported as unknown.

The resource-type facts are from the IAM service authorization reference for
Amazon CloudWatch Logs: ``StartQuery`` and ``DescribeLogStreams`` take a
``log-group``; ``DescribeLogGroups``, ``GetQueryResults`` and ``StopQuery``
take none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "lma-meetingassist-setup-stack" / "template.yaml"

ROLE = "TroubleshootingAgentFunctionRole"
FUNCTION = "TroubleshootingAgentFunction"
LOG_GROUP = "TroubleshootingAgentFunctionLogGroup"

#: Actions whose IAM authorization uses no resource, so a scoped grant denies
#: every call. Each is paired with the reason, for whoever reads a failure.
RESOURCELESS_ACTIONS = {
    "xray:PutTraceSegments": "names no resource; required by active tracing",
    "xray:PutTelemetryRecords": "names no resource; required by active tracing",
}
RESOURCELESS_LOG_ACTIONS = {
    "logs:DescribeLogGroups": (
        "lists the account's log groups and takes no log group as input; the "
        "logGroupNamePrefix in the request filters the response, it is not the "
        "resource authorized against"
    ),
    "logs:GetQueryResults": "takes a query id, not a log group",
    "logs:StopQuery": "takes a query id, not a log group",
}
RESOURCELESS_ACTIONS.update(RESOURCELESS_LOG_ACTIONS)

#: Actions that do take a log group, and so must stay scoped.
SCOPED_LOG_ACTIONS = {"logs:StartQuery"}

#: Nested stacks whose log groups the agent is not expected to read. Empty on
#: purpose: discovery pages every nested stack the parent reports, so anything
#: left out here has to be left out of discovery too, or its groups are listed
#: and then cannot be queried.
NESTED_STACKS_NOT_READABLE: set[str] = set()


class _Loader(yaml.SafeLoader):
    """Parses CloudFormation short-form intrinsics into plain data."""


def _intrinsic(loader: _Loader, suffix: str, node: yaml.Node) -> Any:
    name = "Fn::" + suffix if suffix not in ("Ref", "Condition") else suffix
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_Loader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(name="template", scope="module")
def _template() -> Dict[str, Any]:
    return yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_Loader)


@pytest.fixture(name="role", scope="module")
def _role(template: Dict[str, Any]) -> Dict[str, Any]:
    return template["Resources"][ROLE]["Properties"]


def statements(role: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Every statement in every inline policy on the role."""
    for policy in role.get("Policies", []):
        if not isinstance(policy, dict) or "PolicyDocument" not in policy:
            # A whole policy wrapped in an Fn::If; take the non-NoValue branch.
            for branch in (policy.get("Fn::If") or [])[1:]:
                if isinstance(branch, dict) and "PolicyDocument" in branch:
                    yield from branch["PolicyDocument"]["Statement"]
            continue
        for statement in policy["PolicyDocument"]["Statement"]:
            if "Fn::If" in statement:
                for branch in statement["Fn::If"][1:]:
                    if isinstance(branch, dict) and "Effect" in branch:
                        yield branch
                continue
            yield statement


def actions_of(statement: Dict[str, Any]) -> List[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def resources_of(statement: Dict[str, Any]) -> List[Any]:
    resource = statement.get("Resource", [])
    return [resource] if not isinstance(resource, list) else list(resource)


def rendered_resources(statement: Dict[str, Any]) -> List[str]:
    """Every ARN a statement can resolve to, as the strings in the template.

    An entry may be a plain string, an ``Fn::Sub``, or an ``Fn::If`` whose
    branches are either of those plus ``AWS::NoValue``. Both branches of a
    conditional are returned: each one ships on some deployment, so an
    assertion about the grant has to hold for all of them.
    """
    out: List[str] = []
    pending: List[Any] = resources_of(statement)
    while pending:
        entry = pending.pop()
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict) and "Fn::Sub" in entry:
            out.append(entry["Fn::Sub"])
        elif isinstance(entry, dict) and "Fn::If" in entry:
            pending.extend(entry["Fn::If"][1:])
        elif entry == {"Ref": "AWS::NoValue"}:
            continue
    return out


def statements_granting(role: Dict[str, Any], action: str) -> List[Dict[str, Any]]:
    return [s for s in statements(role) if action in actions_of(s)]


# ── wide enough to work ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("action", "why"), sorted(RESOURCELESS_ACTIONS.items()))
def test_an_action_with_no_resource_type_is_granted_on_all_resources(
    role: Dict[str, Any], action: str, why: str
) -> None:
    """Scoping one of these denies every call, and the tools' own unit tests
    stub the Logs client, so nothing else here would notice. The assertions
    below name the action and the reason.
    """
    granting = statements_granting(role, action)

    assert granting, f"{action} is not granted at all"
    for statement in granting:
        assert resources_of(statement) == ["*"], (
            f"{action} is scoped, which denies every call: it {why}"
        )


def test_the_resourceless_actions_are_the_only_wildcards_on_the_role(
    role: Dict[str, Any],
) -> None:
    """So a future wildcard has to be argued for here rather than appearing."""
    for statement in statements(role):
        if resources_of(statement) != ["*"]:
            continue
        assert set(actions_of(statement)) <= set(RESOURCELESS_ACTIONS), (
            "a new wildcard grant appeared on this role: "
            f"{sorted(set(actions_of(statement)) - set(RESOURCELESS_ACTIONS))}"
        )


# ── narrow enough ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", sorted(SCOPED_LOG_ACTIONS))
def test_an_action_that_takes_a_log_group_stays_scoped(
    role: Dict[str, Any], action: str
) -> None:
    """These are what bound what the agent can actually read."""
    granting = statements_granting(role, action)

    assert granting, f"{action} is not granted at all"
    for statement in granting:
        assert resources_of(statement) != ["*"], f"{action} must stay scoped"


def test_the_log_read_grant_names_nested_stacks_rather_than_the_bare_prefix(
    role: Dict[str, Any],
) -> None:
    """A grant on "/${LMAStackName}-*" would also cover a neighbouring stack
    whose name begins with this one's."""
    rendered = rendered_resources(statements_granting(role, "logs:StartQuery")[0])

    assert not any(r.endswith("log-group:/${LMAStackName}-*") for r in rendered)
    assert any("log-group:/${LMAStackName}-AISTACK-*" in r for r in rendered)
    assert any("log-group:/${LMAStackName}/*" in r for r in rendered)


def test_every_nested_stack_the_deployment_creates_can_be_read(
    role: Dict[str, Any],
) -> None:
    """Discovery pages log groups for every nested stack the parent reports.

    A nested stack missing from this grant is one whose groups are discovered,
    offered to the agent, and then refused — and because a Logs Insights query
    names several groups at once, one unreadable group fails the whole query.
    The agent is told that is "a fault in the tool", so the symptom would be an
    investigation that mysteriously cannot read some components.
    """
    main = yaml.load(
        (REPO / "lma-main.yaml").read_text(encoding="utf-8"), Loader=_Loader
    )
    nested = {
        name
        for name, resource in main["Resources"].items()
        if resource.get("Type") == "AWS::CloudFormation::Stack"
    }
    assert len(nested) >= 12, "the nested stack scan is not seeing lma-main.yaml"

    rendered = rendered_resources(statements_granting(role, "logs:StartQuery")[0])
    granted = {
        r.split("log-group:/${LMAStackName}-", 1)[1].rstrip("-*")
        for r in rendered
        if "log-group:/${LMAStackName}-" in r
    }

    missing = nested - granted - NESTED_STACKS_NOT_READABLE
    assert not missing, (
        "these nested stacks create log groups that discovery will list but "
        f"this role cannot query: {sorted(missing)}. Add a "
        "'log-group:/${LMAStackName}-<LOGICALID>-*' entry for each, or add it "
        "to NESTED_STACKS_NOT_READABLE and exclude it from discovery too."
    )


def test_the_grant_names_no_stack_the_deployment_does_not_create(
    role: Dict[str, Any],
) -> None:
    """The other direction: a stale entry is a grant for nothing."""
    main = yaml.load(
        (REPO / "lma-main.yaml").read_text(encoding="utf-8"), Loader=_Loader
    )
    nested = {
        name
        for name, resource in main["Resources"].items()
        if resource.get("Type") == "AWS::CloudFormation::Stack"
    }

    rendered = rendered_resources(statements_granting(role, "logs:StartQuery")[0])
    granted = {
        r.split("log-group:/${LMAStackName}-", 1)[1].rstrip("-*")
        for r in rendered
        if "log-group:/${LMAStackName}-" in r
        # The MicroVM and vended-states entries are not nested stack prefixes.
        and "lambda-microvms" not in r
        and "vendedlogs" not in r
    }

    assert not granted - nested, f"stale entries: {sorted(granted - nested)}"


def test_no_tool_calls_describe_log_streams(role: Dict[str, Any]) -> None:
    """Dropped rather than carried: the role's own comment says an unused grant
    is one nobody re-reads before relying on it."""
    source = (
        REPO / "lma-meetingassist-setup-stack" / "src" / "troubleshooting_tools.py"
    ).read_text(encoding="utf-8")

    assert "describe_log_streams" not in source
    assert not statements_granting(role, "logs:DescribeLogStreams")


def test_the_microvm_log_groups_are_named_in_full(role: Dict[str, Any]) -> None:
    """"/aws/lambda-microvms/${LMAStackName}-" would match a neighbour's
    "-dev-vp-logs"."""
    rendered = rendered_resources(statements_granting(role, "logs:StartQuery")[0])

    assert any("lambda-microvms/${LMAStackName}-vp-logs" in r for r in rendered)
    assert any("lambda-microvms/${LMAStackName}-asr-logs" in r for r in rendered)


def test_active_tracing_is_matched_by_the_permission_it_needs(
    role: Dict[str, Any], template: Dict[str, Any]
) -> None:
    """`TracingConfig: Mode: Active` with no X-Ray grant records nothing, and
    fails silently — the function looks traced and produces no segments."""
    assert (
        template["Resources"][FUNCTION]["Properties"]["TracingConfig"]["Mode"]
        == "Active"
    )

    for action in ("xray:PutTraceSegments", "xray:PutTelemetryRecords"):
        assert statements_granting(role, action), f"{action} is not granted"


def test_the_role_can_read_records_from_two_named_tables_only(
    role: Dict[str, Any],
) -> None:
    granting = statements_granting(role, "dynamodb:GetItem")

    assert len(granting) == 1
    rendered = rendered_resources(granting[0])
    assert len(rendered) == 2
    assert any("${VirtualParticipantTableName}" in r for r in rendered)
    assert any("${LLMPromptTemplateTableName}" in r for r in rendered)
    assert not any(r.endswith("table/*") for r in rendered)


def test_the_role_reads_its_own_stack_tree_only(role: Dict[str, Any]) -> None:
    granting = statements_granting(role, "cloudformation:DescribeStacks")

    assert len(granting) == 1
    rendered = rendered_resources(granting[0])
    assert rendered == [
        "arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}"
        ":stack/${LMAStackName}/*"
    ]


def test_resolving_nested_stack_names_is_granted_on_the_parent_only(
    role: Dict[str, Any],
) -> None:
    """This is what makes discovery exact; it must not become a wildcard."""
    granting = statements_granting(role, "cloudformation:ListStackResources")

    assert len(granting) == 1
    assert resources_of(granting[0]) != ["*"]


#: Every action the role is allowed to hold. An allowlist rather than a
#: denylist of write prefixes: a denylist cannot distinguish "this action is a
#: read" from "nobody thought of this action", and the whole point of the role
#: is that its surface is enumerable. Adding to this list is the deliberate act
#: that a reviewer should see.
#:
#: Some of these are not reads — see PERMITTED_NON_READS, which is the list
#: that says which. None of them touches the deployment: `appsync:GraphQL`
#: publishes a progress step through one mutation field, `logs:StopQuery`
#: cancels a query this role itself started, and the log and X-Ray writes are
#: this function's own telemetry. Counts are deliberately absent from these
#: comments: a count goes stale the moment an action is added, and the sets
#: below are the authoritative statement either way.
PERMITTED_ACTIONS = {
    "logs:DescribeLogGroups",
    "logs:StartQuery",
    "logs:GetQueryResults",
    "logs:StopQuery",
    # The function writing its own log stream. Granted inline and scoped to
    # one log group rather than through AWSLambdaBasicExecutionRole, which
    # grants the same three actions account-wide — see the role's comment.
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    # What TracingConfig Mode: Active needs to record anything. The function
    # sets it, and without these the traces would be silently empty.
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
    "dynamodb:GetItem",
    "cloudformation:DescribeStacks",
    "cloudformation:ListStackResources",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:GetInferenceProfile",
    "bedrock:ApplyGuardrail",
    "appsync:GraphQL",
    "kms:Decrypt",
    "kms:DescribeKey",
}

#: The permitted actions that are not reads, named so the claim "it reads and
#: does not change anything it diagnoses" can be checked rather than trusted.
#: Narration, cancelling its own query, and its own logs and traces.
PERMITTED_NON_READS = {
    "appsync:GraphQL",
    "logs:StopQuery",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    # What TracingConfig Mode: Active needs to record anything. The function
    # sets it, and without these the traces would be silently empty.
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
}


def test_the_role_holds_only_the_actions_it_is_meant_to(
    role: Dict[str, Any],
) -> None:
    """The role's whole surface, enumerated. A new action fails here."""
    held = {
        action for statement in statements(role) for action in actions_of(statement)
    }

    assert held <= PERMITTED_ACTIONS, (
        f"actions not on the permitted list: {sorted(held - PERMITTED_ACTIONS)}. "
        "If one of these belongs on an otherwise read-only diagnostic role, add "
        "it to PERMITTED_ACTIONS and say why in the template's comment."
    )


def test_every_permitted_action_is_actually_granted(role: Dict[str, Any]) -> None:
    """The other direction: a stale entry would hide a later removal."""
    held = {
        action for statement in statements(role) for action in actions_of(statement)
    }

    assert PERMITTED_ACTIONS <= held, f"never granted: {sorted(PERMITTED_ACTIONS - held)}"


def test_the_role_attaches_no_managed_policy(role: Dict[str, Any]) -> None:
    """Every action this role holds has to be visible in this file.

    A managed policy's contents are not in the template, so attaching one puts
    permissions outside the reach of the assertions here.
    AWSLambdaBasicExecutionRole — the obvious one to reach for — grants
    logs:CreateLogGroup, CreateLogStream and PutLogEvents across the whole
    account, which are writes and are unscoped. The function's own log writes
    are granted inline and scoped instead.
    """
    assert "ManagedPolicyArns" not in role


def test_the_function_can_write_only_its_own_log_stream(
    role: Dict[str, Any],
) -> None:
    for action in ("logs:CreateLogStream", "logs:PutLogEvents"):
        granting = statements_granting(role, action)
        assert granting, f"{action} is not granted; the function cannot log"
        for statement in granting:
            for rendered in rendered_resources(statement):
                assert rendered.endswith(
                    "log-group:/${AWS::StackName}/lambda/TroubleshootingAgentFunction:*"
                ), rendered


def test_the_role_cannot_create_a_log_group(role: Dict[str, Any]) -> None:
    """Not needed, so not granted: the log group is a resource in this
    template and the function reaches it through LoggingConfig."""
    assert not statements_granting(role, "logs:CreateLogGroup")


def test_the_non_read_actions_are_only_the_kinds_the_docs_describe(
    role: Dict[str, Any],
) -> None:
    """Pins the claim the template comment, the docs and the CHANGELOG all make:
    the role reads, and the exceptions are narrating progress, cancelling its
    own query, and writing its own logs and traces.

    The write-prefix list below has to stay broad enough to catch an action
    that is not a read even when it is not called "Put" or "Create" —
    `appsync:GraphQL` is the example that matters, since a GraphQL mutation is
    a write by any reasonable reading and looks like neither.
    """
    write_prefixes = (
        "logs:Put",
        "logs:Create",
        "logs:Delete",
        "logs:Tag",
        "logs:Associate",
        "logs:StopQuery",
        "dynamodb:Put",
        "dynamodb:Update",
        "dynamodb:Delete",
        "dynamodb:BatchWrite",
        "cloudformation:Create",
        "cloudformation:Update",
        "cloudformation:Delete",
        "cloudformation:Execute",
        "appsync:",
        "xray:",
        "iam:",
        "lambda:",
        "s3:Put",
        "s3:Delete",
        "secretsmanager:",
        "sts:",
    )
    held = {
        action for statement in statements(role) for action in actions_of(statement)
    }
    non_reads = {a for a in held if a.startswith(write_prefixes)}

    assert non_reads == PERMITTED_NON_READS, (
        f"the role's non-read surface changed: {sorted(non_reads)}. The role's "
        "own comment, docs/troubleshooting-agent.md and the CHANGELOG all list "
        "the kinds of non-read grant this role holds -- narrating progress, "
        "cancelling its own query, and writing its own logs and traces. Update "
        "all three descriptions if this is intended."
    )


def test_the_role_cannot_invoke_a_lambda_function(role: Dict[str, Any]) -> None:
    """Deliberately absent: no tool needs it, and it is the broadest mutating
    capability an otherwise read-only role could carry."""
    for statement in statements(role):
        for action in actions_of(statement):
            assert not action.startswith("lambda:"), action


def test_the_role_cannot_read_ecs_or_step_functions(role: Dict[str, Any]) -> None:
    """The tools that would use these are not in this release, and an unused
    grant is one nobody re-reads before relying on."""
    for statement in statements(role):
        for action in actions_of(statement):
            assert not action.startswith(("ecs:", "states:")), action


def test_the_appsync_grant_is_one_mutation_field(role: Dict[str, Any]) -> None:
    granting = statements_granting(role, "appsync:GraphQL")

    assert len(granting) == 1
    for rendered in rendered_resources(granting[0]):
        assert rendered.endswith("/types/Mutation/fields/addChatToken")


def test_the_kms_grant_is_conditioned_on_the_services_that_use_it(
    role: Dict[str, Any],
) -> None:
    granting = statements_granting(role, "kms:Decrypt")

    assert len(granting) == 1
    via = granting[0]["Condition"]["StringEquals"]["kms:ViaService"]
    assert len(via) == 2
    assert any("dynamodb" in entry["Fn::Sub"] for entry in via)
    assert any("logs" in entry["Fn::Sub"] for entry in via)


# ── the house conventions ───────────────────────────────────────────────────


def test_the_role_carries_the_permissions_boundary_conditional(
    role: Dict[str, Any],
) -> None:
    boundary = role["PermissionsBoundary"]["Fn::If"]

    assert boundary[0] == "HasPermissionsBoundary"
    assert boundary[1] == {"Ref": "PermissionsBoundaryArn"}


def test_every_arn_in_the_role_is_partition_aware(role: Dict[str, Any]) -> None:
    """A hardcoded arn:aws: breaks in GovCloud and China."""
    for statement in statements(role):
        for rendered in rendered_resources(statement):
            if rendered == "*":
                continue
            assert rendered.startswith("arn:${AWS::Partition}:"), rendered


def test_every_wildcard_grant_has_a_written_cfn_nag_reason(
    template: Dict[str, Any],
) -> None:
    suppressions = template["Resources"][ROLE]["Metadata"]["cfn_nag"][
        "rules_to_suppress"
    ]
    by_id = {entry["id"]: entry.get("reason", "") for entry in suppressions}

    assert "W11" in by_id
    for action in RESOURCELESS_LOG_ACTIONS:
        assert action in by_id["W11"], f"W11's reason does not mention {action}"


def test_the_function_and_its_log_group_share_a_condition(
    template: Dict[str, Any],
) -> None:
    resources = template["Resources"]

    assert (
        resources[FUNCTION]["Condition"]
        == resources[LOG_GROUP]["Condition"]
        == resources[ROLE]["Condition"]
        == "ShouldDeployTroubleshootingAgent"
    )


def test_the_function_waits_for_its_log_group(template: Dict[str, Any]) -> None:
    assert LOG_GROUP in template["Resources"][FUNCTION]["DependsOn"]


def test_the_log_group_is_encrypted_and_expires(template: Dict[str, Any]) -> None:
    properties = template["Resources"][LOG_GROUP]["Properties"]

    assert properties["KmsKeyId"] == {"Ref": "CustomerManagedEncryptionKeyArn"}
    assert properties["RetentionInDays"] == {"Ref": "CloudWatchLogsExpirationInDays"}


def test_an_asynchronous_invocation_is_not_retried(
    template: Dict[str, Any],
) -> None:
    """"A failed run is reported, never retried" has to hold for every way the
    function can be invoked, not only the synchronous one.

    Lambda retries an asynchronous invocation twice by default, which would
    re-run a timed-out diagnosis from the original question — paying again for
    every tool call it already made, for a question whose answer has not
    changed. The checkov reason on the function, the module docstring and the
    docs all state the no-retry property; this is what makes it true.
    """
    configs = [
        resource
        for resource in template["Resources"].values()
        if resource.get("Type") == "AWS::Lambda::EventInvokeConfig"
        and resource.get("Properties", {}).get("FunctionName")
        == {"Ref": FUNCTION}
    ]

    assert len(configs) == 1, "the troubleshooting function has no EventInvokeConfig"
    assert configs[0]["Properties"]["MaximumRetryAttempts"] == 0
    assert configs[0]["Condition"] == "ShouldDeployTroubleshootingAgent"


def test_the_function_timeout_leaves_room_for_the_agents_own_budget(
    template: Dict[str, Any],
) -> None:
    """The agent reports running out of time rather than being killed, so the
    Lambda has to outlast the default wall-clock budget of 240s."""
    assert template["Resources"][FUNCTION]["Properties"]["Timeout"] >= 300
