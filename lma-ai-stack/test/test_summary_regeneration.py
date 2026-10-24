# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Invariants behind on-demand summary regeneration and hand-edited summaries.

The behaviour itself is tested beside the Lambda, in
`source/lambda_functions/summary_regenerator/test_index.py`, and in the UI suite.
What is tested here is the wiring those two cannot reach: the timeouts of one
Lambda waiting on another, the two access rules having to say the same thing in
three languages, and the one place the two halves of the feature share a number.

These are source-level and template-level assertions in the same spirit as
`test_speaker_naming_invokes.py` and `test_ai_stack_template.py` -- they cannot
prove the behaviour, but they do fail if somebody removes a guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC / "schema.graphql"
SUMMARY_VTL = APPSYNC / "updateCallSummaryText.request.vtl"
IDENTITIES_VTL = APPSYNC / "updateSpeakerIdentities.request.vtl"
REGENERATOR = AI_STACK / "source" / "lambda_functions" / "summary_regenerator" / "index.py"
CALL_FIELDS = (
    AI_STACK
    / "source"
    / "lambda_layers"
    / "transcript_enrichment_layer"
    / "graphql_helpers"
    / "call_fields.py"
)
SUMMARY_STATE_JS = AI_STACK / "source" / "ui" / "src" / "components" / "common" / "summary-state.js"


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
def resources() -> dict:
    return yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]


@pytest.fixture(scope="module")
def regenerator() -> str:
    return REGENERATOR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def summary_vtl() -> str:
    return SUMMARY_VTL.read_text(encoding="utf-8")


# ── the two Lambdas' timeouts have to agree ─────────────────────────────────


def _read_timeout(resources: dict) -> int:
    env = resources["SummaryRegeneratorFunction"]["Properties"]["Environment"]["Variables"]
    return int(env["SUMMARY_READ_TIMEOUT"])


def _constant(regenerator: str, name: str) -> int:
    return int(re.search(rf"^{name} = (\d+)$", regenerator, re.M).group(1))


def test_the_regenerator_stops_waiting_for_the_summariser_before_its_own_deadline(
    resources: dict,
) -> None:
    """Both functions stop at Lambda's 900s ceiling, so waiting the summariser
    out in full is not possible: this one would be killed mid-wait having
    written nothing, and the meeting would stay marked as regenerating until
    that mark went stale. It gives up with time in hand instead."""
    regen = resources["SummaryRegeneratorFunction"]["Properties"]["Timeout"]
    assert _read_timeout(resources) < regen


def test_the_regenerator_keeps_enough_of_its_deadline_to_record_an_outcome(
    resources: dict, regenerator: str
) -> None:
    """The whole budget: the wait for the summariser plus the worst case of
    storing the result has to fit inside this function's timeout, or a run that
    went wrong ends as a killed Lambda and no explanation on the meeting."""
    attempts = _constant(regenerator, "APPSYNC_WRITE_ATTEMPTS")
    per_attempt = _constant(regenerator, "APPSYNC_WRITE_TIMEOUT_SECONDS")
    backoff_base = _constant(regenerator, "APPSYNC_WRITE_BACKOFF_SECONDS")
    # Backoff doubles and does not follow the final attempt.
    backoff = sum(backoff_base * 2**attempt for attempt in range(attempts - 1))
    worst_case = _read_timeout(resources) + attempts * per_attempt + backoff
    regen = resources["SummaryRegeneratorFunction"]["Properties"]["Timeout"]
    assert worst_case < regen, (
        f"a run can take {worst_case}s but the function stops at {regen}s, so "
        f"the failure would never be recorded"
    )


def test_the_stale_mark_outlasts_the_longest_possible_run(
    resources: dict, regenerator: str
) -> None:
    """Otherwise a run still going is treated as abandoned and a second one is
    accepted beside it.

    One invocation's Timeout is the whole of a run, which is what the test below
    about the retry policy is here to keep true: platform retries would make a
    single run several invocations long, with the backoff between them on top.
    """
    regen = resources["SummaryRegeneratorFunction"]["Properties"]["Timeout"]
    stale = int(re.search(r'SUMMARY_REGENERATION_STALE_SECONDS", "(\d+)"', regenerator).group(1))
    assert stale > regen


def test_the_second_leg_is_not_retried_by_the_platform(resources: dict) -> None:
    """A retry would re-run every model call for a summary already computed.

    The only error that reaches Lambda from the second leg is a failure to store
    a finished result, and write_summary retries that inside the one invocation.
    This also keeps a run to a single invocation, which the stale window above
    is sized against.
    """
    config = resources["SummaryRegeneratorFunction"]["Properties"]["EventInvokeConfig"]
    assert config["MaximumRetryAttempts"] == 0


def test_the_regenerator_cannot_retry_the_summariser(regenerator: str) -> None:
    """Which is what makes abandoning it safe: the orchestrator's longer socket
    timeout for the speaker-naming invoke exists to stop a retry re-running the
    callee, and there are no retries here to stop."""
    config = re.search(r"read_timeout=.*?retries=\{[^}]*\}", regenerator, re.S)
    assert config, "the dedicated client config is gone"
    assert '"max_attempts": 1' in config.group(0)


# ── least privilege ─────────────────────────────────────────────────────────


def _statements(role: dict) -> list[dict]:
    out = []
    for policy in role["Properties"].get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            # !If-wrapped statements appear as a one-key mapping; take both arms.
            if "Fn::If" in statement:
                out.extend(arm for arm in statement["Fn::If"][1:] if isinstance(arm, dict))
            else:
                out.append(statement)
    return out


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def test_the_regenerator_can_only_write_the_one_graphql_field(resources: dict) -> None:
    """Scoped the way the speaker name resolver is: the Lambda writes with its
    own IAM credentials, which take the backend branch of the mutation's access
    condition, so the field it may call is the boundary."""
    role = resources["SummaryRegeneratorFunctionExecutionRole"]
    graphql = [s for s in _statements(role) if "appsync:GraphQL" in _actions(s)]
    assert len(graphql) == 1
    resource = graphql[0]["Resource"]["Fn::Sub"]
    assert resource.endswith("/types/Mutation/fields/updateCallSummaryText")


def test_the_regenerator_cannot_write_to_the_table(resources: dict) -> None:
    """Its reach into the table is a single read. Every write goes through the
    AppSync mutation instead, which is both where the access rule is applied and
    what makes open meeting pages show a new summary without being reloaded."""
    role = resources["SummaryRegeneratorFunctionExecutionRole"]
    dynamo = [a for s in _statements(role) for a in _actions(s) if a.startswith("dynamodb:")]
    assert dynamo == ["dynamodb:GetItem"], dynamo


def test_the_regenerator_has_no_model_access_of_its_own(resources: dict) -> None:
    """It never prompts a model: summarisation, including assembling the
    prompts, stays entirely in the configured summary function."""
    role = resources["SummaryRegeneratorFunctionExecutionRole"]
    assert not [a for s in _statements(role) for a in _actions(s) if a.startswith("bedrock:")]


def test_the_regenerator_role_carries_the_permissions_boundary(resources: dict) -> None:
    for name in ("SummaryRegeneratorFunctionExecutionRole", "AppSyncSummaryRegeneratorLambdaRole"):
        boundary = resources[name]["Properties"]["PermissionsBoundary"]
        assert boundary["Fn::If"][0] == "HasPermissionsBoundary"


# ── the access rule, stated the same way in every language ──────────────────


def test_the_summary_write_has_its_own_request_template(resources: dict) -> None:
    """The shared updateCall template SETs every key in its input and applies no
    per-caller condition, which is safe only for the IAM-only mutations that use
    it. This field is written from a browser."""
    resolver = resources["UpdateCallSummaryTextAppSyncResolver"]["Properties"]
    assert resolver["FieldName"] == "updateCallSummaryText"
    assert resolver["RequestMappingTemplateS3Location"].endswith(
        "updateCallSummaryText.request.vtl"
    )


def test_the_summary_write_restricts_a_signed_in_caller(summary_vtl: str) -> None:
    """Admin, the meeting's owner, or a user it has been shared with -- the same
    rule getCall applies on the way out, expressed as a DynamoDB condition so
    the check and the write are one operation."""
    assert "#Owner = :caller OR contains(#SharedWith, :caller)" in summary_vtl
    assert 'groups.contains("Admin")' in summary_vtl
    assert '"condition"' in summary_vtl


def test_the_summary_write_states_the_rule_exactly_as_the_identity_write_does(
    summary_vtl: str,
) -> None:
    """Two templates drifting apart would leave two different answers to the
    same question about the same meeting."""
    identities = IDENTITIES_VTL.read_text(encoding="utf-8")
    clause = "#Owner = :caller OR contains(#SharedWith, :caller)"
    detection = '$context.identity.userArn.contains(":assumed-role/")'
    for source in (summary_vtl, identities):
        assert clause in source
        assert detection in source


def test_the_two_lambdas_apply_one_access_rule_line_for_line() -> None:
    """`caller_may_edit` is duplicated, so it has to be duplicated exactly.

    The two functions answer the same question about the same meeting rows, from
    two mutations the same button set reaches, and a reader of either docstring
    is told they agree. Only the docstrings differ, naming each one's own
    backend caller, so the comparison is of everything after them.
    """
    bodies = []
    for name in ("summary_regenerator", "speaker_name_resolver"):
        source = (AI_STACK / "source" / "lambda_functions" / name / "index.py").read_text(
            encoding="utf-8"
        )
        rule = source[source.index("def caller_may_edit(") :]
        rule = rule[: rule.index("\ndef ")]
        # Everything after the docstring's closing quotes.
        bodies.append(rule[rule.index('"""', rule.index('"""') + 3) + 3 :].strip())
    assert bodies[0] == bodies[1], "the two copies of caller_may_edit have drifted apart"


def test_the_share_list_rule_is_stated_where_each_path_applies_it(summary_vtl: str) -> None:
    """Both paths say what `SharedWith` shape their test is exact for.

    `contains` in a DynamoDB condition is a whole-element test on the List form
    `shareCall` writes and a substring test on the legacy comma-separated String,
    which a condition expression cannot split; the Lambda reads the attribute in
    Python and compares entry by entry in both. The fixture table covering the
    Python side is `SHARED_WITH_CASES` in the regenerator's own test file. What
    is pinned here is that neither place claims an exactness it does not have.
    """
    assert "cannot split a string" in summary_vtl
    for name in ("summary_regenerator", "speaker_name_resolver"):
        source = (AI_STACK / "source" / "lambda_functions" / name / "index.py").read_text(
            encoding="utf-8"
        )
        rule = source[source.index("def caller_may_edit(") :]
        rule = rule[: rule.index("\ndef ")]
        # Whitespace-normalised: the phrase is in a docstring and so wraps.
        assert "entry by entry" in " ".join(rule.split()), name


def test_the_summary_write_only_touches_the_two_summary_attributes(summary_vtl: str) -> None:
    """The write is scoped to the summary: these are the only two attributes it
    assigns, and the rest of the meeting row keeps whatever the pipeline put
    there."""
    # From where the update expression starts being built, so the access
    # condition's own `#Owner = :caller` is not mistaken for an assignment.
    update = summary_vtl[summary_vtl.index('$expression = "SET"') :]
    assigned = set(re.findall(r"#(\w+) = :", update))
    assert assigned == {"CallSummaryText", "CallSummaryState"}, assigned


def test_the_regeneration_applies_the_same_rule_in_the_lambda(regenerator: str) -> None:
    """A Lambda resolver has no DynamoDB condition to lean on."""
    rule = regenerator[regenerator.index("def caller_may_edit(") :]
    rule = rule[: rule.index("\ndef ")]
    assert '"Admin" in groups' in rule
    assert 'meeting.get("Owner") == username' in rule
    assert "shared_with" in rule
    # Access is a match between two names, and both of the fields matched against
    # are absent on some rows, so a caller with no resolvable name is settled
    # before either field is read.
    assert rule.index("if not username:") < rule.index('meeting.get("Owner")')
    # Checked before anything is queued or written.
    start = regenerator[regenerator.index("def start(") :]
    assert start.index("caller_may_edit(") < start.index("starter_client.invoke(")


def test_the_two_lambda_resolvers_carry_only_the_arguments_and_the_identity() -> None:
    """Both resolvers name their payload rather than forwarding the context.

    Those two fields are the whole of what either Lambda reads, so the
    invocation carries the data they work from and nothing else.
    """
    for name in ("regenerateCallSummary.js", "resolveSpeakerNames.js"):
        source = (APPSYNC / name).read_text(encoding="utf-8")
        assert "payload: { arguments: ctx.arguments, identity: ctx.identity }" in source, name
        assert "payload: ctx," not in source, name


def test_each_resolver_lambda_logs_the_fields_that_identify_the_work() -> None:
    """Which meeting and which leg, named rather than serialised."""
    functions = AI_STACK / "source" / "lambda_functions"
    for name in ("summary_regenerator", "speaker_name_resolver"):
        source = (functions / name / "index.py").read_text(encoding="utf-8")
        handler = source[source.index("def handler(") :]
        assert "json.dumps(event" not in handler, name
        assert 'event.get("Trigger")' in handler, name


# ── reaching the browser ────────────────────────────────────────────────────


def test_both_summary_mutations_are_reachable_by_a_signed_in_user() -> None:
    schema = SCHEMA.read_text(encoding="utf-8")
    for field in ("updateCallSummaryText", "regenerateCallSummary"):
        line = next(line for line in schema.splitlines() if line.strip().startswith(f"{field}("))
        assert "@aws_cognito_user_pools" in line
        assert "@aws_iam" in line


def test_a_summary_write_is_published_to_the_meeting_subscription() -> None:
    """Regenerating returns before the run finishes, so the subscription is the
    only way the new summary reaches the page that asked for it."""
    schema = SCHEMA.read_text(encoding="utf-8")
    lines = schema.splitlines()
    on_update = next(i for i, line in enumerate(lines) if line.strip().startswith("onUpdateCall("))
    subscribe = next(line for line in lines[on_update:] if "@aws_subscribe" in line)
    assert '"updateCallSummaryText"' in subscribe


def test_the_pipelines_own_call_mutations_do_not_select_the_new_field() -> None:
    """The shared selector is built against the schema the API is *running*, so a
    field it names has to exist in the deployed schema before the layer carrying
    it ships. Leaving the new field out keeps the layer and the schema
    independently deployable, exactly as SpeakerIdentities does; the browser
    carries the value over instead, which is what setCallsDeduped is for."""
    assert "schema.Call.CallSummaryState" not in CALL_FIELDS.read_text(encoding="utf-8")


def test_the_browser_carries_the_summary_state_over_a_pipeline_update() -> None:
    """The other half of the arrangement above: those mutations are published to
    onUpdateCall, and AppSync delivers a field the subscription selects but the
    mutation did not as null, so the stored value has to survive the merge."""
    hook = (AI_STACK / "source" / "ui" / "src" / "hooks" / "use-calls-graphql-api.js").read_text(
        encoding="utf-8"
    )
    assert "CallSummaryState: call.CallSummaryState || existing?.CallSummaryState," in hook


def test_the_stale_run_cut_off_is_the_same_on_both_sides(regenerator: str) -> None:
    """The Lambda uses it to decide whether to accept a run and the UI uses it to
    decide whether to offer one, so a disagreement shows either an enabled
    button that is refused or a disabled one that would have been accepted."""
    seconds = int(re.search(r'SUMMARY_REGENERATION_STALE_SECONDS", "(\d+)"', regenerator).group(1))
    js = SUMMARY_STATE_JS.read_text(encoding="utf-8")
    ui_seconds = int(re.search(r"STALE_RUN_MS = (\d+) \* 1000", js).group(1))
    assert seconds == ui_seconds


def test_the_clock_skew_allowance_is_the_same_on_both_sides(regenerator: str) -> None:
    """The other end of the same window: a stamp further ahead than this is not
    a clock disagreeing, and is read as finished rather than holding the meeting
    until that time arrives. Both sides have to draw the line in one place."""
    seconds = int(re.search(r"CLOCK_SKEW_ALLOWANCE_SECONDS = (\d+)", regenerator).group(1))
    js = SUMMARY_STATE_JS.read_text(encoding="utf-8")
    ui_seconds = int(re.search(r"CLOCK_SKEW_ALLOWANCE_MS = (\d+) \* 1000", js).group(1))
    assert seconds == ui_seconds


def test_the_summary_state_vocabulary_matches_on_both_sides(regenerator: str) -> None:
    """The Lambda writes these strings and the UI reads them."""
    js = SUMMARY_STATE_JS.read_text(encoding="utf-8")
    for name, value in (
        ("SOURCE_GENERATED", "generated"),
        ("SOURCE_MANUAL", "manual"),
        ("STATUS_RUNNING", "running"),
        ("STATUS_FAILED", "failed"),
        ("STATUS_IDLE", "idle"),
    ):
        assert f'{name} = "{value}"' in regenerator
        assert f"{name} = '{value}'" in js


# ── the Knowledge Base snapshot ─────────────────────────────────────────────


def test_the_run_sends_no_prompt_override_to_the_summariser(regenerator: str) -> None:
    """A `Prompt` override would bypass the stored prompt templates and, in the
    Bedrock summariser, skip the snapshot of the transcript and summary written
    to S3 for the Bedrock Knowledge Base -- which regenerating is partly there
    to refresh."""
    invoke = regenerator[regenerator.index("def invoke_summary_function(") :]
    invoke = invoke[: invoke.index("\ndef ")]
    assert '"CallId": call_id' in invoke
    assert "Prompt" not in invoke


def test_the_run_asks_the_summariser_to_re_read_its_configuration(regenerator: str) -> None:
    """The panel promises the prompt templates as they are now.

    The summariser caches them for a few minutes, which is right for the
    end-of-meeting runs it does unattended and wrong for a button somebody
    pressed straight after saving a prompt. Asserted on both sides, because the
    field only works if the sender and the reader spell it the same way.
    """
    invoke = regenerator[regenerator.index("def invoke_summary_function(") :]
    invoke = invoke[: invoke.index("\ndef ")]
    assert '"RefreshConfiguration": True' in invoke

    summariser = (
        AI_STACK / "source" / "lambda_functions" / "bedrock_summary_lambda" / "index.py"
    ).read_text(encoding="utf-8")
    assert 'event.get("RefreshConfiguration")' in summariser
    assert "CONFIG_STORE.invalidate()" in summariser


def test_the_summariser_still_only_writes_the_snapshot_without_an_override() -> None:
    """The property the test above relies on, asserted where it actually lives."""
    summariser = (
        AI_STACK / "source" / "lambda_functions" / "bedrock_summary_lambda" / "index.py"
    ).read_text(encoding="utf-8")
    handler = summariser[summariser.index("def handler(") :]
    assert "if not prompt_override:" in handler
    assert handler.index("if not prompt_override:") < handler.index("write_to_s3(")
