# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Static gates on the application metrics LMA publishes (no AWS).

A companion to `test_main_stack_observability.py`, which gates the alarms on
metrics AWS services publish. These gate the other half: metrics LMA emits
itself, where there is one more way for everything to look correct and measure
nothing.

That way is the namespace. Every emitter is told its CloudWatch namespace by a
`METRIC_NAMESPACE` environment variable, and the dashboard and the alarms select
on the stack's name. An emitter whose variable is unset keeps running perfectly
and silently publishes nothing; an emitter whose variable is set to the wrong
thing publishes to a namespace nothing reads. Neither failure is visible from
the component -- the only symptom is an empty widget and an alarm reading OK,
since both alarms on these metrics treat missing data as not breaching. That is
exactly what a quiet deployment looks like. Nothing in a
build or a deploy checks that an emitter and the things reading it agree on a
namespace, so the first group of tests below does.

The second group is parity: a metric nobody can see is not worth emitting, and a
metric that records a *swallowed* failure -- an exception the code catches,
records and does not raise -- needs an alarm rather than a widget, because the
number is the only notice anyone gets.

The third re-renders the dashboard body and parses it. The body is inline JSON
inside the list form of `Fn::Sub`, so a stray comma or an unbalanced brace is a
syntactically valid template that fails at `CREATE` time, 35-40 minutes into a
deploy.

The fourth checks that the TypeScript half of the catalogue agrees with the
Python half. The transcriber is not a Lambda and cannot import the Python
module, so the three names it emits are written in both places.

Run via `make test-ai-stack` or pytest directly.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO_ROOT / "lma-main.yaml"
AI_TEMPLATE = REPO_ROOT / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml"
TRANSCRIBER_TEMPLATE = (
    REPO_ROOT / "lma-websocket-transcriber-stack" / "deployment" / "lma-websocket-transcriber.yaml"
)
TRANSCRIBER_CATALOGUE_TS = (
    REPO_ROOT
    / "lma-websocket-transcriber-stack"
    / "source"
    / "app"
    / "src"
    / "metrics"
    / "catalogue.ts"
)
MONITORING_DOC = REPO_ROOT / "docs" / "monitoring.md"

# The catalogue is the single source of truth for the metric names, and it
# imports nothing outside the standard library precisely so that this gate can
# read it without the Lambda layer's dependencies installed.
LAYER_ROOT = REPO_ROOT / "lma-ai-stack" / "source" / "lambda_layers" / "transcript_enrichment_layer"
if str(LAYER_ROOT) not in sys.path:
    sys.path.insert(0, str(LAYER_ROOT))

from metrics_utils import catalogue  # noqa: E402

ALARM_TYPE = "AWS::CloudWatch::Alarm"

# How the namespace must be written. Both nested stacks that emit are children,
# so `AWS::StackName` inside them is a generated name the parent cannot predict
# and the dashboard does not select on; `LMAStackName` is the parameter each is
# given the parent's name through.
EXPECTED_NAMESPACE_VALUE = {"Ref": "LMAStackName"}

# Powertools' own name for the same setting. aws-lambda-powertools is on the
# transcript enrichment layer, so anything later written with its `Metrics`
# class reads this instead. Where both are present they must agree.
POWERTOOLS_NAMESPACE_VAR = "POWERTOOLS_METRICS_NAMESPACE"


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


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def main_template() -> dict:
    return _load(MAIN_TEMPLATE)


@pytest.fixture(scope="module")
def ai_template() -> dict:
    return _load(AI_TEMPLATE)


@pytest.fixture(scope="module")
def transcriber_template() -> dict:
    return _load(TRANSCRIBER_TEMPLATE)


@pytest.fixture(scope="module")
def alarms(main_template: dict) -> dict:
    return {
        name: body
        for name, body in main_template["Resources"].items()
        if body.get("Type") == ALARM_TYPE
    }


@pytest.fixture(scope="module")
def dashboard_body(main_template: dict) -> dict:
    """The dashboard's JSON, with every `Fn::Sub` variable given a stand-in.

    Rendering rather than reading: what the gate needs to check is the document
    CloudFormation produces, and the raw string is not valid JSON on its own
    only because of the `${...}` placeholders.
    """
    dashboards = [
        body
        for body in main_template["Resources"].values()
        if body.get("Type") == "AWS::CloudWatch::Dashboard"
    ]
    assert len(dashboards) == 1, f"expected exactly one dashboard, found {len(dashboards)}"
    template, variables = dashboards[0]["Properties"]["DashboardBody"]["Fn::Sub"]

    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key == "AWS::StackName":
            return STACK_NAME
        if key == "AWS::Region":
            return "us-east-1"
        if key == "TranscribeQuotaThreshold":
            # A number in the JSON, not a string, so it must render as digits.
            return "20"
        assert key in variables, (
            f"the dashboard body uses ${{{key}}} but the Fn::Sub variable map does not "
            f"define it, so CloudFormation fails the template"
        )
        return f"resolved-{key}"

    rendered = re.sub(r"\$\{([^}]+)\}", substitute, template)
    try:
        return json.loads(rendered)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"the dashboard body is not valid JSON once rendered, which fails at CREATE "
            f"time rather than at lint time: {error}"
        ) from error


STACK_NAME = "lma-gate-test"


# ── emitters: the namespace variable, and the IAM that would need it ────────


# Every resource that emits application metrics, with the template it lives in
# and the kind of resource it is. Spelled out rather than discovered, so that a
# component which stops being an emitter has to be removed from here
# deliberately rather than falling out of the gate silently.
EMITTERS = {
    "CallEventProcessorFunction": (AI_TEMPLATE, catalogue.Component.CALL_EVENT_PROCESSOR),
    "TranscriberWebsocketTaskDef": (
        TRANSCRIBER_TEMPLATE,
        catalogue.Component.WEBSOCKET_TRANSCRIBER,
    ),
}


def _lambda_environment(resource: dict) -> dict:
    return resource["Properties"].get("Environment", {}).get("Variables", {})


def _container_environment(resource: dict) -> dict:
    """The one container definition's environment, as a name-to-value mapping."""
    containers = resource["Properties"]["ContainerDefinitions"]
    assert len(containers) == 1, "the task definition has more than one container"
    return {entry["Name"]: entry["Value"] for entry in containers[0].get("Environment", [])}


def _environment_of(logical_id: str) -> dict:
    template_path, _ = EMITTERS[logical_id]
    resource = _load(template_path)["Resources"][logical_id]
    if resource["Type"] == "AWS::ECS::TaskDefinition":
        return _container_environment(resource)
    return _lambda_environment(resource)


def test_the_emitters_this_gate_knows_about_cover_the_catalogue():
    """Guards the tests below against checking a component nobody emits from.

    Every component in the catalogue must appear here, so that adding a metric
    for a new component without wiring its namespace fails immediately.
    """
    covered = {component for _, component in EMITTERS.values()}
    assert covered == set(catalogue.Component), (
        f"these components publish metrics but no emitter resource is listed for them: "
        f"{sorted(c.value for c in set(catalogue.Component) - covered)}"
    )


@pytest.mark.parametrize("logical_id", sorted(EMITTERS))
def test_every_emitter_is_told_its_metric_namespace(logical_id: str):
    """The failure this whole gate exists for.

    An emitter with no `METRIC_NAMESPACE` runs normally and publishes nothing.
    Nothing in the component reports it, nothing in the deploy reports it, and
    the dashboard looks the same as it does on a quiet day.
    """
    environment = _environment_of(logical_id)
    assert catalogue.NAMESPACE_ENV_VAR in environment, (
        f"{logical_id} publishes application metrics but has no "
        f"{catalogue.NAMESPACE_ENV_VAR}, so it would publish none of them and say nothing"
    )


@pytest.mark.parametrize("logical_id", sorted(EMITTERS))
def test_every_emitter_uses_the_parent_stack_name_as_its_namespace(logical_id: str):
    """`LMAStackName`, not `AWS::StackName`.

    Both emitters are in nested stacks, where `AWS::StackName` is a name
    CloudFormation generates. The dashboard and the alarms are in the parent and
    select on the parent's name, so an emitter using its own stack's name
    publishes a complete, healthy, unread set of metrics.
    """
    value = _environment_of(logical_id)[catalogue.NAMESPACE_ENV_VAR]
    assert value == EXPECTED_NAMESPACE_VALUE, (
        f"{logical_id} sets {catalogue.NAMESPACE_ENV_VAR} to {value!r}; the dashboard and "
        f"alarms read the parent stack's name, so it must be {EXPECTED_NAMESPACE_VALUE!r}"
    )


@pytest.mark.parametrize("logical_id", sorted(EMITTERS))
def test_no_emitter_has_two_disagreeing_names_for_one_namespace(logical_id: str):
    """`POWERTOOLS_METRICS_NAMESPACE` is the same setting under another name.

    aws-lambda-powertools is a dependency of the transcript enrichment layer, so
    anything later written with its `Metrics` class reads that variable rather
    than this one. Two names for one namespace that disagree publish half the
    metrics where nothing is looking, and the template is the only place the
    disagreement is visible.
    """
    environment = _environment_of(logical_id)
    if POWERTOOLS_NAMESPACE_VAR not in environment:
        return
    assert environment[POWERTOOLS_NAMESPACE_VAR] == environment[catalogue.NAMESPACE_ENV_VAR], (
        f"{logical_id} sets {POWERTOOLS_NAMESPACE_VAR} and {catalogue.NAMESPACE_ENV_VAR} to "
        f"different namespaces: {environment[POWERTOOLS_NAMESPACE_VAR]!r} and "
        f"{environment[catalogue.NAMESPACE_ENV_VAR]!r}"
    )


def test_the_transcriber_is_given_the_parent_stack_name():
    """The parameter the transcriber's namespace resolves through.

    It is the one nested stack the parent did not already pass its own name to,
    so the namespace depends on a parameter added for it. A parameter declared
    in the child and not passed by the parent resolves to its default -- and
    with no default, fails the deploy, which is the better of the two outcomes
    but still worth pinning here.
    """
    child = _load(TRANSCRIBER_TEMPLATE)
    assert "LMAStackName" in child["Parameters"], (
        "the transcriber template no longer declares LMAStackName, which its "
        "METRIC_NAMESPACE resolves through"
    )
    assert "Default" not in child["Parameters"]["LMAStackName"], (
        "LMAStackName must have no default: a default would let the parent forget to "
        "pass it and have the task publish to a plausible-looking wrong namespace"
    )
    parent = _load(MAIN_TEMPLATE)
    passed = parent["Resources"]["WEBSOCKETTRANSCRIBERSTACK"]["Properties"]["Parameters"]
    assert passed.get("LMAStackName") == {"Ref": "AWS::StackName"}, (
        "lma-main.yaml must pass its own name to the transcriber stack as LMAStackName; "
        f"it passes {passed.get('LMAStackName')!r}"
    )


@pytest.mark.parametrize("template_path", [AI_TEMPLATE, TRANSCRIBER_TEMPLATE, MAIN_TEMPLATE])
def test_a_put_metric_data_grant_is_scoped_and_accompanied_by_the_variable(template_path: Path):
    """A `PutMetricData` grant must be scoped, and must come with the variable.

    Nothing grants `cloudwatch:PutMetricData` today, and nothing should: these
    metrics are Embedded Metric Format records that CloudWatch Logs extracts,
    and AWS documents that as needing only the log-writing permission the
    component already has. This gate is here for the change that adds one
    anyway, and it asserts both halves of the pair:

    * the grant carries the `cloudwatch:namespace` condition, since without it
      the role may write to any namespace in the account;
    * the resource holding it also sets `METRIC_NAMESPACE`.

    The second half is the one worth a test. A grant conditioned on a namespace,
    on a component that was never told which namespace to use, denies every
    emission at runtime -- and the denial is not an error anybody sees, because
    the caller is a metric emitter that must not fail the work it measures.
    """
    text = template_path.read_text()
    if "cloudwatch:PutMetricData" not in text:
        return

    document = _load(template_path)
    offenders: list[str] = []
    for logical_id, resource in document.get("Resources", {}).items():
        statements = _put_metric_data_statements(resource)
        if not statements:
            continue
        for statement in statements:
            condition = statement.get("Condition", {}).get("StringEquals", {})
            if condition.get("cloudwatch:namespace") != EXPECTED_NAMESPACE_VALUE:
                offenders.append(
                    f"{logical_id}: PutMetricData is not conditioned on "
                    f"cloudwatch:namespace = {EXPECTED_NAMESPACE_VALUE!r}"
                )
        environment = _lambda_environment(resource) or _ecs_environment_or_empty(resource)
        if catalogue.NAMESPACE_ENV_VAR not in environment:
            offenders.append(
                f"{logical_id}: holds a PutMetricData grant but has no "
                f"{catalogue.NAMESPACE_ENV_VAR}, so every emission would be denied"
            )
    assert not offenders, "\n".join(offenders)


def _ecs_environment_or_empty(resource: dict) -> dict:
    if resource.get("Type") != "AWS::ECS::TaskDefinition":
        return {}
    return _container_environment(resource)


def _put_metric_data_statements(resource: dict) -> list[dict]:
    """Every IAM statement in this resource that allows `PutMetricData`."""
    found = []
    for statement in _walk_statements(resource):
        actions = statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if any(
            isinstance(action, str) and action.startswith("cloudwatch:PutMetricData")
            for action in actions
        ):
            found.append(statement)
    return found


def _walk_statements(node) -> list[dict]:
    """Every mapping that looks like an IAM statement, anywhere under `node`."""
    found = []
    if isinstance(node, dict):
        if "Effect" in node and "Action" in node:
            found.append(node)
        for value in node.values():
            found.extend(_walk_statements(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_statements(value))
    return found


# ── the call sites, where deleting one line stops everything silently ──────


CALL_EVENT_PROCESSOR_HANDLER = (
    REPO_ROOT
    / "lma-ai-stack"
    / "source"
    / "lambda_functions"
    / "call_event_processor"
    / "lambda_function.py"
)


def test_the_call_event_processor_flushes_its_metrics_on_every_exit_path():
    """Without this one call, the function measures everything and publishes none of it.

    Gated by reading the source rather than by invoking the handler, because
    importing it needs `aiohttp` and the rest of the Lambda's runtime
    dependencies, which the test environment does not install -- the suite that
    does drive the handler skips itself for exactly that reason, so it cannot be
    what protects this.

    `finally` specifically, not merely somewhere in the handler. This handler
    raises deliberately when a failure cannot be expressed as a Kinesis sequence
    number, and that invocation's measurements -- which include the segment
    write failures the whole metric exists for -- are the ones that would be
    lost to a trailing call.
    """
    import ast

    tree = ast.parse(CALL_EVENT_PROCESSOR_HANDLER.read_text())
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "handler"
    ]
    assert handlers, "call_event_processor/lambda_function.py declares no handler()"

    def flushes(nodes) -> bool:
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "flush"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "METRICS"
            for body in nodes
            for node in ast.walk(body)
        )

    finallys = [
        node for handler in handlers for node in ast.walk(handler) if isinstance(node, ast.Try)
    ]
    assert any(flushes(node.finalbody) for node in finallys), (
        "handler() does not call METRICS.flush() from a finally block, so an invocation "
        "that raises publishes nothing it measured"
    )


# Where each metric helper has to be called from for the metric to exist at all.
# Checked as source text because the alternative is no check: these are single
# statements in a long-lived server and a Lambda, and deleting one produces a
# component that works perfectly and is no longer measured. Nothing else in the
# build would notice.
TRANSCRIBER_APP = REPO_ROOT / "lma-websocket-transcriber-stack" / "source" / "app" / "src"
REQUIRED_CALL_SITES = {
    "noteStreamingSessionStarted": TRANSCRIBER_APP / "index.ts",
    "noteStreamingSessionEnded": TRANSCRIBER_APP / "index.ts",
    # The one function both transcription engines write a segment through, which
    # is why the first segment of a meeting is recognised there and not twice.
    "noteTranscriptSegment": TRANSCRIBER_APP / "calleventdata" / "transcribe.ts",
}


@pytest.mark.parametrize("helper", sorted(REQUIRED_CALL_SITES))
def test_the_transcriber_still_calls_each_metric_helper(helper: str):
    """Only that the call is there, not that it is imported.

    A call to something that is not imported fails `tsc`, which `make
    lint-typescript` runs, so there is nothing here for this to add. A call that
    is deleted or commented out compiles perfectly, which is the case this
    covers.
    """
    source_file = REQUIRED_CALL_SITES[helper]
    assert source_file.is_file(), f"{source_file} does not exist"
    source = source_file.read_text()
    uncommented = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )
    assert f"{helper}(" in uncommented, (
        f"{source_file.name} no longer calls {helper}, so the metrics it emits are not "
        f"published and nothing else reports that"
    )


# ── parity: emitted, alarmed, graphed ──────────────────────────────────────


def _dashboard_metric_names(body: dict) -> set[str]:
    """Every metric name the dashboard reads out of this stack's namespace."""
    names = set()
    for widget in body["widgets"]:
        for entry in widget.get("properties", {}).get("metrics", []) or []:
            # A metric entry is [namespace, name, ...dimension pairs, options].
            if len(entry) >= 2 and entry[0] == STACK_NAME:
                names.add(entry[1])
    return names


# Both ways of writing this stack's name as a namespace. Accepting only `!Ref`
# would make an alarm written with `!Sub` invisible to the gates below rather
# than failing them -- it would read a metric LMA emits while being treated as
# though it read an AWS-published one, which is the quietest way for the parity
# and late-datapoint gates to stop covering an alarm.
STACK_NAME_NAMESPACES = (
    {"Ref": "AWS::StackName"},
    {"Fn::Sub": "${AWS::StackName}"},
)


def _is_this_stacks_namespace(value) -> bool:
    return value in STACK_NAME_NAMESPACES


def _alarm_metric_names(properties: dict) -> set[str]:
    """Metric names one alarm reads out of this stack's namespace.

    Both shapes an alarm can take: a single metric named directly on the alarm,
    and the `Metrics` list a metric-math alarm uses. Anything reading only the
    first silently stops covering an alarm the moment it grows a second term.
    """
    names = set()
    if _is_this_stacks_namespace(properties.get("Namespace")):
        names.add(properties["MetricName"])
    for metric in properties.get("Metrics", []) or []:
        stat = metric.get("MetricStat", {}).get("Metric", {})
        if _is_this_stacks_namespace(stat.get("Namespace")):
            names.add(stat["MetricName"])
    return names


def _alarmed_metric_names(alarms: dict) -> set[str]:
    """Every metric name any alarm reads out of this stack's namespace."""
    return set().union(*(_alarm_metric_names(b["Properties"]) for b in alarms.values()))


def test_no_alarm_names_this_stacks_namespace_in_a_form_the_gates_miss(alarms: dict):
    """Both accepted forms resolve identically; anything else is not recognised.

    The gates above find an alarm on an LMA-published metric by matching its
    `Namespace` against the two forms of the stack's name. A third form -- a
    `Fn::Join`, or an `Fn::Sub` with surrounding text -- resolves to the right
    namespace at deploy time and is silently skipped here, so the alarm would
    exist, work, and be covered by nothing.
    """
    unrecognised = {}
    for name, body in alarms.items():
        namespace = body["Properties"].get("Namespace")
        if not isinstance(namespace, dict):
            continue
        rendered = str(namespace)
        if "AWS::StackName" in rendered and not _is_this_stacks_namespace(namespace):
            unrecognised[name] = namespace
    assert not unrecognised, (
        f"these alarms build this stack's namespace in a form the parity gates do not "
        f"recognise, so they are covered by none of them: {unrecognised}. Use "
        f"`!Ref AWS::StackName`, or add the form to STACK_NAME_NAMESPACES."
    )


def test_every_emitted_metric_is_on_the_dashboard(dashboard_body: dict):
    """A metric nobody can see was not worth the code that emits it."""
    missing = sorted(set(catalogue.BY_NAME) - _dashboard_metric_names(dashboard_body))
    assert not missing, (
        f"these metrics are emitted but appear on no dashboard widget, so nothing shows "
        f"them: {missing}"
    )


def test_every_metric_recording_a_swallowed_failure_has_an_alarm(alarms: dict):
    """A widget is not enough for these.

    Each of these records a condition the code catches, records and does not
    raise, so no AWS-published metric moves and no notification is sent. A
    number nobody is paged on, recording an error nobody saw, is not
    observability.
    """
    alarmed = _alarmed_metric_names(alarms)
    missing = sorted(set(catalogue.must_be_alarmed()) - alarmed)
    assert not missing, (
        f"these metrics record a failure the code swallows and have no alarm, so the only "
        f"notice of it is somebody looking at a graph: {missing}"
    )


def test_no_alarm_reads_a_metric_nothing_emits(alarms: dict):
    """An alarm on a metric nobody publishes never leaves its no-data state.

    With `TreatMissingData: notBreaching` that state is OK, so the alarm is
    indistinguishable from one that is simply quiet and reports healthy through
    exactly the failure it was added for. A misspelled metric name is all it
    takes, and neither cfn-lint nor the deploy checks one.
    """
    unknown = sorted(_alarmed_metric_names(alarms) - set(catalogue.BY_NAME))
    assert not unknown, (
        f"these alarms read metrics in this stack's namespace that nothing in the metric "
        f"catalogue emits: {unknown}"
    )


def test_no_dashboard_widget_reads_a_metric_nothing_emits(dashboard_body: dict):
    unknown = sorted(_dashboard_metric_names(dashboard_body) - set(catalogue.BY_NAME))
    assert not unknown, (
        f"these dashboard widgets read metrics in this stack's namespace that nothing "
        f"emits, so they are permanently empty: {unknown}"
    )


def test_every_alarm_on_an_emitted_metric_tolerates_a_late_datapoint(alarms: dict):
    """These metrics arrive through CloudWatch Logs, not `PutMetricData`.

    AWS recommends an M-of-N alarm wherever the log flush interval is not under
    the application's control, which on Lambda and on the ECS `awslogs` driver
    it is not: a datapoint can land in the period after the one it belongs to.
    An alarm requiring every period to breach can miss the event entirely.

    Both alarm shapes are examined. Reading only the top-level `MetricName`
    would let a metric-math alarm on one of these metrics escape the check
    without failing it -- the same hole, in the same gate, that
    `STACK_NAME_NAMESPACES` exists to close for the namespace.
    """
    emitted = set(catalogue.BY_NAME)
    too_strict = {}
    for name, body in alarms.items():
        properties = body["Properties"]
        if not (_alarm_metric_names(properties) & emitted):
            continue
        periods = properties.get("EvaluationPeriods")
        to_alarm = properties.get("DatapointsToAlarm")
        if to_alarm is None or periods is None or to_alarm >= periods:
            too_strict[name] = f"{to_alarm} of {periods}"
    assert not too_strict, (
        f"these alarms read a metric delivered through CloudWatch Logs but require every "
        f"evaluation period to breach, so a late datapoint can hide the event: {too_strict}"
    )


# ── the dashboard body, as CloudFormation will render it ───────────────────


def test_the_dashboard_body_renders_to_valid_json(dashboard_body: dict):
    """The fixture raises if it does not; this names the failure.

    The body is inline JSON in the list form of `Fn::Sub`, so a stray comma is a
    valid template that fails at CREATE, after the nested stacks have taken
    35-40 minutes to succeed.
    """
    assert dashboard_body["widgets"], "the dashboard has no widgets"


def test_every_widget_declares_a_position_and_a_type(dashboard_body: dict):
    """Two widgets sharing a position overlap, and the console says nothing.

    Positions are absolute, so inserting a widget anywhere but the bottom means
    moving every widget below it by hand -- and getting one wrong hides a widget
    rather than failing anything.
    """
    seen: dict[tuple, str] = {}
    for index, widget in enumerate(dashboard_body["widgets"]):
        assert widget.get("type") in {"metric", "log", "text", "alarm"}, (
            f"widget {index} has an unrecognised type {widget.get('type')!r}"
        )
        for axis in ("x", "y", "width", "height"):
            assert axis in widget, f"widget {index} has no {axis}"
        position = (widget["x"], widget["y"])
        title = widget.get("properties", {}).get("title", widget["type"])
        assert position not in seen, (
            f"widget {title!r} is at the same position as {seen[position]!r}, so one hides "
            f"the other"
        )
        seen[position] = title


def test_no_widget_runs_off_the_dashboard(dashboard_body: dict):
    """A dashboard is 24 columns wide; a widget past the edge is clipped."""
    for widget in dashboard_body["widgets"]:
        assert widget["x"] + widget["width"] <= 24, (
            f"widget at x={widget['x']} width={widget['width']} extends past column 24"
        )


def test_every_annotation_value_is_a_number(dashboard_body: dict):
    """An annotation whose value rendered as a string is dropped by the console
    without an error, taking the alarm threshold off the graph with it."""
    for widget in dashboard_body["widgets"]:
        annotations = widget.get("properties", {}).get("annotations", {})
        for annotation in annotations.get("horizontal", []) or []:
            assert isinstance(annotation.get("value"), (int, float)), (
                f"annotation {annotation.get('label')!r} has a non-numeric value "
                f"{annotation.get('value')!r}"
            )


def test_every_alarm_threshold_annotation_names_an_alarm_that_exists(
    dashboard_body: dict, alarms: dict
):
    """The annotations are how an operator reads a graph against its alarm, so a
    label naming an alarm that was renamed or removed is worse than none."""
    declared = {
        body["Properties"]["AlarmName"]["Fn::Sub"].replace("${AWS::StackName}", STACK_NAME)
        for body in alarms.values()
    }
    for widget in dashboard_body["widgets"]:
        annotations = widget.get("properties", {}).get("annotations", {})
        for annotation in annotations.get("horizontal", []) or []:
            label = annotation.get("label", "")
            named = [name for name in declared if name in label]
            if STACK_NAME in label:
                assert named, f"annotation {label!r} names no alarm this template declares"


# ── the two halves of the catalogue ────────────────────────────────────────


def test_the_typescript_catalogue_lists_exactly_what_the_transcriber_publishes():
    """The transcriber cannot import the Python catalogue, so it has a copy.

    A name added on one side only produces either a metric the gates above
    cannot see or an emitter refusing a name the dashboard reads, and both look
    like a working deployment.
    """
    source = TRANSCRIBER_CATALOGUE_TS.read_text()
    # The names as the TypeScript catalogue's entries declare them, which is the
    # list the emitter actually consults -- a constant declared and left out of
    # CATALOGUE would be refused at runtime.
    listed = set(re.findall(r"\{\s*name:\s*([A-Z_]+),\s*unit:\s*'(\w+)'\s*\}", source))
    constants = dict(re.findall(r"export const ([A-Z_]+) = '(\w+)';", source))

    resolved = {}
    for constant, unit in listed:
        assert constant in constants, f"CATALOGUE references {constant}, which is not declared"
        resolved[constants[constant]] = unit

    expected = {
        name: catalogue.BY_NAME[name].unit
        for name in catalogue.names_for(catalogue.Component.WEBSOCKET_TRANSCRIBER)
    }
    assert resolved == expected, (
        f"src/metrics/catalogue.ts and metrics_utils/catalogue.py disagree about what the "
        f"transcriber publishes: TypeScript has {resolved}, Python expects {expected}"
    )


def test_the_typescript_catalogue_uses_the_same_environment_variable():
    source = TRANSCRIBER_CATALOGUE_TS.read_text()
    match = re.search(r"METRIC_NAMESPACE_ENV_VAR = '(\w+)'", source)
    assert match, "src/metrics/catalogue.ts no longer declares METRIC_NAMESPACE_ENV_VAR"
    assert match.group(1) == catalogue.NAMESPACE_ENV_VAR, (
        f"the transcriber reads {match.group(1)!r} but the templates set "
        f"{catalogue.NAMESPACE_ENV_VAR!r}"
    )


# ── the docs, against the catalogue ────────────────────────────────────────


# A row of the metric table in docs/monitoring.md: the name in backticks in the
# first cell, then the unit in the second.
DOC_METRIC_ROW = re.compile(r"^\|\s*`(\w+)`\s*\|\s*(\w+)\s*\|", re.MULTILINE)


def test_the_monitoring_doc_tabulates_every_emitted_metric():
    """The metric table is where an operator finds out what a number means.

    Checked against the table rather than against the whole page. Several of
    these metrics are also discussed in the prose on the same page, so asserting
    only that the name appears somewhere in the document would pass with the
    table row deleted.

    The unit is compared too, because a row stating the wrong unit is worse than
    a missing one -- it is a number the reader will act on in the wrong scale.
    """
    rows = dict(DOC_METRIC_ROW.findall(MONITORING_DOC.read_text()))
    documented = {name: unit for name, unit in rows.items() if name in catalogue.BY_NAME}
    expected = {name: metric.unit for name, metric in catalogue.BY_NAME.items()}

    missing = sorted(set(expected) - set(documented))
    assert not missing, (
        f"these metrics have no row in the metric table in docs/monitoring.md, so nothing "
        f"tells an operator what the number means: {missing}"
    )
    wrong_unit = {
        name: f"doc says {documented[name]}, catalogue says {expected[name]}"
        for name in expected
        if documented[name] != expected[name]
    }
    assert not wrong_unit, f"the metric table states the wrong unit: {wrong_unit}"
