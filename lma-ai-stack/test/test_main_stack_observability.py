# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Static tests for the operational alarms and dashboard in `lma-main.yaml` (no AWS).

These live next to the other template tests because `make test-ai-stack` collects
this whole directory, so a test added here runs in CI without anyone having to
name it.

What they protect is specific to alerting: an alarm that is wired up wrongly
stays green and says nothing, which is the one failure mode an alerting stack
cannot afford and the one a deploy will never reveal. So each gate below asserts
a property that, if broken, would leave an alarm that exists and never notifies:

* a notification arrives as the alarm name plus the `AlarmDescription`, nothing
  else, so an empty description leaves an operator with a kebab-case string and
  no runbook — and CloudWatch rejects a description over 1024 characters at
  create time, 35-40 minutes into a deploy;
* an alarm with no `AlarmActions`, or one pointing somewhere other than
  `AlertsTopic`, changes state in the console and emails nobody;
* `TreatMissingData` left unset defaults to `missing`, which for several of
  these metrics means the alarm can neither fire nor clear;
* the alerts topic is encrypted with the customer-managed key, and CloudWatch
  must hold `kms:GenerateDataKey*`/`kms:Decrypt` on that key to publish to it.
  Without the grant every alarm reaches ALARM in the console while the
  notification is dropped. The grant must not sit behind a `Fn::If`, because the
  alarms can be switched on by a later stack update.

Also asserted: the alarm count stated in prose in `docs/monitoring.md` and
`docs/well-architected.md` matches the template. Those docs write the total in
digits (`12 alarms`) and every other count in words, so the regex below can tell
the authoritative number from an incidental one.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO_ROOT / "lma-main.yaml"
AI_TEMPLATE = REPO_ROOT / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"
MONITORING_DOC = REPO_ROOT / "docs" / "monitoring.md"
WELL_ARCHITECTED_DOC = REPO_ROOT / "docs" / "well-architected.md"

ALERTS_TOPIC = "AlertsTopic"
ALARM_TYPE = "AWS::CloudWatch::Alarm"

# CloudWatch's documented ceiling for AlarmDescription.
MAX_ALARM_DESCRIPTION_CHARS = 1024

# Prose counts are only authoritative in digit form; see the module docstring.
PROSE_ALARM_COUNT = re.compile(r"\b(\d+)\s+(?:CloudWatch\s+)?alarms\b")


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
def main_template() -> dict:
    return yaml.load(MAIN_TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def ai_template() -> dict:
    return yaml.load(AI_TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def alarms(main_template: dict) -> dict:
    return {
        name: body
        for name, body in main_template["Resources"].items()
        if body.get("Type") == ALARM_TYPE
    }


def _resolve_sub(value, parameters: dict) -> str:
    """Render an `Fn::Sub` (or plain string) to its longest plausible text.

    Only used for length checking, so a `${Ref}` whose value is not knowable
    statically is stood in for by the longest thing CloudFormation could put
    there. Parameter defaults are used where they exist, because that is what a
    default deployment actually produces.
    """
    if isinstance(value, str):
        template, local_vars = value, {}
    elif isinstance(value, dict) and "Fn::Sub" in value:
        inner = value["Fn::Sub"]
        if isinstance(inner, str):
            template, local_vars = inner, {}
        else:
            template, local_vars = inner[0], inner[1]
    else:
        raise AssertionError(f"AlarmDescription is neither a string nor Fn::Sub: {value!r}")

    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key in local_vars:
            return "resolved-value"
        default = parameters.get(key, {}).get("Default")
        if default is not None:
            return str(default)
        # A physical id or an ARN; 128 characters is a generous stand-in.
        return "x" * 128

    return re.sub(r"\$\{([^}]+)\}", substitute, template)


def _referenced_topics(actions) -> list[str]:
    if actions is None:
        return []
    return [
        action["Ref"]
        for action in actions
        if isinstance(action, dict) and isinstance(action.get("Ref"), str)
    ]


# ── the collection itself ───────────────────────────────────────────────────


def test_the_template_declares_the_operational_alarms_and_dashboard(
    main_template: dict, alarms: dict
):
    """Guards every other test here against collecting nothing.

    They all iterate over the alarms the loader produced, so alarms moved to
    another template, or a loader that parsed `Resources` into an empty mapping,
    would otherwise leave this file green having checked nothing.
    """
    assert len(alarms) >= 10, f"only found {len(alarms)} alarms in lma-main.yaml"
    resources = main_template["Resources"]
    assert resources[ALERTS_TOPIC]["Type"] == "AWS::SNS::Topic"
    dashboards = [
        name
        for name, body in resources.items()
        if body.get("Type") == "AWS::CloudWatch::Dashboard"
    ]
    assert len(dashboards) == 1, f"expected exactly one dashboard, found {dashboards}"


# ── what a notification actually carries ────────────────────────────────────


def test_every_alarm_has_a_description_within_the_cloudwatch_limit(
    main_template: dict, alarms: dict
):
    parameters = main_template.get("Parameters", {})
    missing = []
    too_long = []
    for name, body in alarms.items():
        description = body["Properties"].get("AlarmDescription")
        if not description:
            missing.append(name)
            continue
        resolved = _resolve_sub(description, parameters)
        collapsed = " ".join(resolved.split())
        if not collapsed:
            missing.append(name)
        elif len(collapsed) > MAX_ALARM_DESCRIPTION_CHARS:
            too_long.append(f"{name} ({len(collapsed)} chars)")
    assert not missing, f"alarms with no AlarmDescription: {missing}"
    assert not too_long, (
        "AlarmDescription longer than CloudWatch accepts, which fails the deploy: "
        f"{too_long}"
    )


def test_every_alarm_notifies_the_alerts_topic(alarms: dict):
    wrong = {}
    for name, body in alarms.items():
        topics = _referenced_topics(body["Properties"].get("AlarmActions"))
        if topics != [ALERTS_TOPIC]:
            wrong[name] = topics
    assert not wrong, f"alarms whose AlarmActions are not exactly [{ALERTS_TOPIC}]: {wrong}"


def test_every_alarm_that_notifies_on_recovery_uses_the_same_topic(alarms: dict):
    """`OKActions` is optional, but where set it must reach the same subscribers."""
    wrong = {}
    for name, body in alarms.items():
        actions = body["Properties"].get("OKActions")
        if actions is None:
            continue
        topics = _referenced_topics(actions)
        if topics != [ALERTS_TOPIC]:
            wrong[name] = topics
    assert not wrong, f"alarms whose OKActions are not exactly [{ALERTS_TOPIC}]: {wrong}"


def test_every_alarm_sets_treat_missing_data_explicitly(alarms: dict):
    """Unset means `missing`, under which several of these can neither fire nor clear."""
    allowed = {"notBreaching", "breaching", "ignore", "missing"}
    unset = [name for name, body in alarms.items() if "TreatMissingData" not in body["Properties"]]
    assert not unset, f"alarms with no explicit TreatMissingData: {unset}"
    invalid = {
        name: body["Properties"]["TreatMissingData"]
        for name, body in alarms.items()
        if body["Properties"]["TreatMissingData"] not in allowed
    }
    assert not invalid, f"alarms with an unrecognised TreatMissingData: {invalid}"


def test_alarm_names_are_unique_and_namespaced_to_the_stack(alarms: dict):
    """Alarm names are account-and-region unique, so two LMA stacks must not collide."""
    names = []
    for name, body in alarms.items():
        alarm_name = body["Properties"].get("AlarmName")
        assert isinstance(alarm_name, dict) and "Fn::Sub" in alarm_name, (
            f"{name} has no Fn::Sub AlarmName, so two deployments in one account collide"
        )
        rendered = alarm_name["Fn::Sub"]
        assert rendered.startswith("${AWS::StackName}-"), (
            f"{name} AlarmName is not prefixed with the stack name: {rendered}"
        )
        names.append(rendered)
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"duplicate AlarmName values: {duplicates}"


# ── the topic, and whether anything is listening ────────────────────────────


def test_the_alerts_topic_has_a_subscription_declared_in_the_template(main_template: dict):
    """A topic with no subscription path is an alerting stack that alerts nobody.

    The subscription may carry a `Condition` — a deployment can legitimately
    start with no subscriber — but the resource has to exist, so that turning
    alerting on is a parameter change rather than a code change.
    """
    subscriptions = [
        name
        for name, body in main_template["Resources"].items()
        if body.get("Type") == "AWS::SNS::Subscription"
        and body.get("Properties", {}).get("TopicArn") == {"Ref": ALERTS_TOPIC}
    ]
    inline = main_template["Resources"][ALERTS_TOPIC]["Properties"].get("Subscription")
    assert subscriptions or inline, (
        f"no AWS::SNS::Subscription targets {ALERTS_TOPIC} and the topic declares none inline"
    )


def test_the_alerts_topic_is_encrypted_with_the_customer_managed_key(main_template: dict):
    topic = main_template["Resources"][ALERTS_TOPIC]["Properties"]
    key = topic.get("KmsMasterKeyId")
    # The short form `!GetAtt X.Y` parses to a string and the long form to a
    # two-element list; both are accepted so reformatting the template does not
    # fail this.
    assert key in (
        {"Fn::GetAtt": "CustomerManagedEncryptionKey.Arn"},
        {"Fn::GetAtt": ["CustomerManagedEncryptionKey", "Arn"]},
    ), "the alerts topic must use the stack's customer-managed key, as every other topic does"


def test_the_key_policy_grants_cloudwatch_unconditionally(main_template: dict):
    """CloudWatch is the publisher when an alarm notifies the encrypted topic.

    SNS requires the publishing principal itself to hold `kms:GenerateDataKey*`
    and `kms:Decrypt` on the topic's key. The grant is asserted to sit directly
    in the statement list rather than inside an `Fn::If`, because
    `EnableOperationalAlarms` can be turned on by a later stack update and a
    grant that was skipped at create time would not then appear.
    """
    key = main_template["Resources"]["CustomerManagedEncryptionKey"]
    statements = key["Properties"]["KeyPolicy"]["Statement"]
    assert isinstance(statements, list), "the key policy statement list is conditional as a whole"

    expected_principal = {"Fn::Sub": "cloudwatch.${AWS::URLSuffix}"}
    matching = [
        statement
        for statement in statements
        if isinstance(statement, dict)
        and statement.get("Principal", {}).get("Service") == expected_principal
    ]
    assert matching, (
        "no key policy statement grants the cloudwatch.${AWS::URLSuffix} service principal; "
        "a literal service hostname would also fail this, and would not resolve in GovCloud"
    )
    for statement in matching:
        assert "Fn::If" not in str(statement.get("Effect")), "the grant's Effect is conditional"
        assert statement["Effect"] == "Allow"
        actions = statement["Action"]
        assert "kms:Decrypt" in actions, "the CloudWatch grant is missing kms:Decrypt"
        assert any(action.startswith("kms:GenerateDataKey") for action in actions), (
            "the CloudWatch grant is missing kms:GenerateDataKey*"
        )


# ── cross-template wiring ───────────────────────────────────────────────────


def test_every_nested_stack_output_the_alarms_read_is_declared(ai_template: dict):
    """A renamed AI stack output fails the parent's deploy, not this stack's.

    The alarms and the dashboard resolve metric dimensions through
    `!GetAtt AISTACK.Outputs.<Name>`, which CloudFormation only rejects when the
    parent updates — after the nested stack has already succeeded.
    """
    main = MAIN_TEMPLATE.read_text()
    consumed = set(re.findall(r"AISTACK\s*\.\s*Outputs\s*\.\s*(\w+)", main))
    declared = set(ai_template.get("Outputs", {}))
    observability_outputs = {
        "CallEventProcessorFunctionName",
        "CallEventProcessorLogGroupName",
        "CallEventProcessorDiscardedRecordsQueueName",
        "StaleMeetingReaperFunctionName",
        "AppSyncApiId",
    }
    assert observability_outputs <= consumed, (
        "lma-main.yaml no longer reads outputs the alarms need: "
        f"{sorted(observability_outputs - consumed)}"
    )
    assert observability_outputs <= declared, (
        "the AI stack no longer declares outputs lma-main.yaml reads: "
        f"{sorted(observability_outputs - declared)}"
    )


def test_metric_math_alarms_fill_missing_datapoints(alarms: dict):
    """A null in any term nulls the whole expression, and the alarm stops evaluating.

    Every compound alarm here combines metrics that are each legitimately sparse
    — a stream with no traffic, a service that publishes nothing while idle — so
    an unfilled term means an alarm that silently cannot reach a state.
    """
    unfilled = {}
    for name, body in alarms.items():
        for metric in body["Properties"].get("Metrics", []) or []:
            expression = metric.get("Expression")
            if expression and "FILL(" not in expression:
                unfilled[f"{name}/{metric['Id']}"] = expression
    assert not unfilled, f"metric math expressions with an unfilled term: {unfilled}"


# ── the docs, against the template ──────────────────────────────────────────


def test_the_docs_do_not_overstate_the_number_of_alarms(alarms: dict):
    expected = len(alarms)
    drift = {}
    for doc in (MONITORING_DOC, WELL_ARCHITECTED_DOC):
        assert doc.is_file(), f"{doc.name} is referenced by this test but does not exist"
        for stated in PROSE_ALARM_COUNT.findall(doc.read_text()):
            if int(stated) != expected:
                drift.setdefault(doc.name, []).append(stated)
    assert not drift, (
        f"lma-main.yaml declares {expected} alarms, but these docs state another number "
        f"in digits: {drift}. Counts other than the total should be written in words."
    )


def test_the_monitoring_doc_names_every_alarm(alarms: dict):
    """The alarm table is the runbook index, so an alarm missing from it is invisible."""
    doc = MONITORING_DOC.read_text()
    missing = []
    for name, body in alarms.items():
        rendered = body["Properties"]["AlarmName"]["Fn::Sub"]
        kebab = rendered.replace("${AWS::StackName}-", "")
        if kebab not in doc:
            missing.append(f"{name} ({kebab})")
    assert not missing, f"alarms not documented in docs/monitoring.md: {missing}"


def test_the_monitoring_doc_is_listed_in_the_docs_index():
    index = (REPO_ROOT / "docs" / "INDEX.md").read_text()
    assert "monitoring.md" in index, "docs/monitoring.md is not linked from docs/INDEX.md"
