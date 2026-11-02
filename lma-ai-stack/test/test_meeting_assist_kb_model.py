# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The Meeting Assistant's knowledge-base generation model (no AWS).

A Bedrock Knowledge Base generates its answer with a model named by `modelArn` on
the RetrieveAndGenerate call, and it supplies the orchestration and generation
prompt templates that model is driven with. Those templates do not cover every
model the Converse API the agent runs on will accept: a model they do not cover
is refused with a ValidationException asking for custom templates for both
Orchestration and Generation.

**Which models are usable is a property of the individual model, not of its
generation.** `us.anthropic.claude-opus-4-1-20250805-v1:0` is refused and
`us.anthropic.claude-opus-4-6-v1` is accepted, and both are Claude 4; five of the
six Claude 5.5 ids were measured -- all three `global.` forms, and the `us.`
Haiku and Sonnet -- and all five were refused. The inference-profile prefix makes
no difference either way. `us.anthropic.claude-opus-5-5` is the sixth and the one
Claude 5.5 id nobody called, so it is absent from KB_MODELS_VERIFIED_REFUSED
below; it is withheld at runtime by the generation default in
`kb_model_supports_retrieve_and_generate`, which is a default for unchecked ids
rather than a measurement.

There is therefore no rule to derive the list from, which is why the
list is a record of measurements -- see KB_MODELS_VERIFIED_WORKING below -- and
why `test_the_knowledge_base_choices_are_exactly_the_verified_set` asserts the
choices the configuration schema offers match that record exactly. Adding a model
to the agent's list does not make it usable here: somebody has to check it first,
record it below, and only then can the setting offer it.

So the two models are configured separately, as two settings of the
`language_models` configuration section -- `meetingAssistModelId` for the agent,
`knowledgeBaseModelId` for the knowledge bases -- and this file asserts:

* the knowledge-base setting offers exactly the ids verified against a live
  knowledge base, all of which LMA also supports for the agent, and none that the
  function's own guard would refuse at runtime;
* its shipped default is one of those ids and is the same as the function's own
  fallback;
* the function reads the knowledge-base model from `knowledgeBaseModelId` and the
  agent's from `meetingAssistModelId`, without the two being crossed;
* the two knowledge-base tools build their `modelArn` from the knowledge-base
  model, are not offered to the agent at all when that model cannot generate, and
  return something the agent cannot read as retrieved material when a call fails.

The last group imports the Lambda's own module. `strands` is not installed here
(it ships in a Lambda layer), so a stand-in providing its `tool` decorator is
registered first; the decorator the real SDK applies returns a callable wrapper,
and the stand-in returns the function itself, which is what lets these tests call
a tool body directly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[2]
FUNCTION_SRC = REPO / "lma-meetingassist-setup-stack" / "src"
# The AI stack's configuration layer, which the function is deployed with and
# imports lma_config from.
CONFIGURATION_LAYER = REPO / "lma-ai-stack" / "source" / "lambda_layers" / "configuration_layer"
LANGUAGE_MODELS_SCHEMA = CONFIGURATION_LAYER / "lma_config" / "schemas" / "language_models.json"
SETTING_DEFAULTS = CONFIGURATION_LAYER / "lma_config" / "setting_defaults.json"

LANGUAGE_MODELS = "language_models"
AGENT_MODEL_SETTING = "meetingAssistModelId"
KB_MODEL_SETTING = "knowledgeBaseModelId"

# ── the measurement record ──────────────────────────────────────────────────
#
# Every id here was checked by issuing a RetrieveAndGenerate call against a live
# transcript knowledge base in us-west-2 on 2026-10-21. This record is the reason
# the knowledge-base setting offers what it offers;
# test_the_knowledge_base_choices_are_exactly_the_verified_set ties the two
# together, so a model cannot be offered without being added here, and cannot be
# added here without somebody having made the call.

# RetrieveAndGenerate returned an answer.
KB_MODELS_VERIFIED_WORKING = (
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-opus-4-6-v1",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-opus-4-6-v1",
)

# RetrieveAndGenerate returned `ValidationException: Custom prompt templates must
# be provided for both Orchestration and Generation when using the selected
# model.` Opus 4.1 is the one that shows the refusal is per model rather than per
# generation: it is Claude 4, like the accepted Opus 4.6, and its inference
# profile exists in the account this was measured in, so it is being refused
# rather than being unavailable.
KB_MODELS_VERIFIED_REFUSED = (
    "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "global.anthropic.claude-haiku-5-5",
    "global.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-opus-5-5",
    "us.anthropic.claude-haiku-5-5",
    "us.anthropic.claude-sonnet-5-5",
)

# RetrieveAndGenerate returned `ResourceNotFoundException`, because the account
# measured in has no inference profile for this model at all, so whether a
# knowledge base would accept it is unknown. Unknown is treated as not offered,
# for the same reason the Nova models are not offered: an unverified value in
# this setting is the failure it exists to prevent. It remains selectable for
# the agent, where it is reached through Converse and is unaffected by any of
# this.
KB_MODELS_UNVERIFIABLE = ("us.anthropic.claude-opus-4-20250514-v1:0",)


# ── the configuration section ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def model_settings() -> dict:
    return json.loads(LANGUAGE_MODELS_SCHEMA.read_text())["properties"]


@pytest.fixture(scope="module")
def kb_model_choices(model_settings: dict) -> list:
    return model_settings[KB_MODEL_SETTING]["enum"]


@pytest.fixture(scope="module")
def kb_model_default() -> str:
    return json.loads(SETTING_DEFAULTS.read_text())[LANGUAGE_MODELS][KB_MODEL_SETTING]["default"]


def test_the_knowledge_base_model_is_a_setting_of_its_own(model_settings: dict):
    """Guards the rest of this file against passing by collecting nothing."""
    assert KB_MODEL_SETTING in model_settings, (
        f"language_models.json no longer declares {KB_MODEL_SETTING}; the knowledge bases "
        "would be back on the agent's model"
    )
    assert KB_MODEL_SETTING != AGENT_MODEL_SETTING
    assert AGENT_MODEL_SETTING in model_settings
    assert len(model_settings[KB_MODEL_SETTING]["enum"]) >= 5


def test_the_knowledge_base_choices_are_exactly_the_verified_set(kb_model_choices: list):
    """The setting offers the verified ids and nothing else.

    This is the test that stops a model being offered for retrieval because it was
    added to the agent's list. There is no rule it could be derived from -- the
    refusal is per model, with Claude Opus 4.1 refused and Claude Opus 4.6
    accepted -- so the only honest gate is "somebody made the call and wrote down
    what happened". Equality in both directions is deliberate: a new offered value
    fails until it is recorded as verified above, and a value dropped from the
    record fails until it is also dropped from the setting.
    """
    offered = set(kb_model_choices)
    verified = set(KB_MODELS_VERIFIED_WORKING)
    assert offered == verified, (
        f"offered without a recorded RetrieveAndGenerate check: {sorted(offered - verified)}; "
        f"verified but no longer offered: {sorted(verified - offered)}"
    )


def test_every_verified_refused_model_is_absent_from_the_knowledge_base_choices(
    kb_model_choices: list,
):
    """Stated separately from the equality above, because it is the specific harm.

    An id in this list is one a live knowledge base answered with a
    ValidationException, so offering it would reproduce exactly the failure this
    setting exists to prevent.
    """
    refused = sorted(set(kb_model_choices) & set(KB_MODELS_VERIFIED_REFUSED))
    assert not refused, f"{KB_MODEL_SETTING} offers models a knowledge base refuses: {refused}"


def test_a_model_with_no_inference_profile_measured_is_not_offered(kb_model_choices: list):
    """Unknown is not the same as working.

    These returned ResourceNotFoundException because the account measured in has
    no inference profile for them, which says nothing about whether a knowledge
    base would accept them. They stay out until that is known, on the same footing
    as the Nova models.
    """
    unverifiable = sorted(set(kb_model_choices) & set(KB_MODELS_UNVERIFIABLE))
    assert not unverifiable, (
        f"{KB_MODEL_SETTING} offers models whose knowledge-base behaviour is "
        f"unmeasured: {unverifiable}"
    )


def test_no_offered_model_would_be_refused_by_the_functions_own_guard(
    kb_model_choices: list, strands_module
):
    """The schema and the function have to agree on what is usable.

    The function withholds a knowledge-base tool whose model it believes cannot
    generate. If that belief disagreed with the setting, an administrator could
    select a model the Configuration page offers and silently get no tool.
    """
    refused = [
        value
        for value in kb_model_choices
        if not strands_module.kb_model_supports_retrieve_and_generate(value)
    ]
    assert not refused, f"offered by the setting but withheld by the function: {refused}"


def test_knowledge_base_model_choices_are_a_subset_of_the_agent_model_choices(
    model_settings: dict,
):
    """A knowledge-base choice LMA does not support for the agent is untested ground.

    Every id here is one the deployment already exercises on the Converse path,
    which is what makes the shorter list a restriction rather than a separate,
    unvalidated model list.
    """
    kb_choices = set(model_settings[KB_MODEL_SETTING]["enum"])
    agent_choices = set(model_settings[AGENT_MODEL_SETTING]["enum"])
    extra = sorted(kb_choices - agent_choices)
    assert not extra, f"offered for the knowledge base but not for the agent: {extra}"


def test_the_knowledge_base_model_defaults_to_a_verified_model(kb_model_default: str):
    assert kb_model_default in KB_MODELS_VERIFIED_WORKING, (
        f"{KB_MODEL_SETTING} defaults to {kb_model_default!r}, which has not been verified "
        "against a live knowledge base"
    )


def test_the_knowledge_base_model_default_matches_the_functions_fallback(
    kb_model_default: str, kb_model_choices: list, strands_module
):
    """One default, written down twice, pinned so the copies cannot drift.

    setting_defaults.json is the default an ordinary deployment starts with. The
    function's `DEFAULT_KB_MODEL_ID` is reached only when the configuration
    section cannot be read, which is exactly why a disagreement would go
    unnoticed: it would surface only in the case nobody exercises. The fallback
    must also be a value the schema accepts, or the function could generate with
    a model the Configuration page would refuse to save.
    """
    assert strands_module.DEFAULT_KB_MODEL_ID == kb_model_default, (
        f"DEFAULT_KB_MODEL_ID is {strands_module.DEFAULT_KB_MODEL_ID!r} but "
        f"setting_defaults.json defaults {KB_MODEL_SETTING} to {kb_model_default!r}"
    )
    assert strands_module.DEFAULT_KB_MODEL_ID in kb_model_choices


def _settings_read(strands_module, monkeypatch, reader) -> list:
    """The (section, setting) pairs `reader` asks the configuration for."""
    read: list = []

    def fake_read_setting(section, setting, **_kwargs):
        read.append((section, setting))
        return f"{section}/{setting}"

    monkeypatch.setattr(strands_module, "read_setting", fake_read_setting)
    reader()
    return read


def test_the_function_reads_the_knowledge_base_model_from_its_own_setting(
    strands_module, monkeypatch
):
    read = _settings_read(strands_module, monkeypatch, strands_module.configured_kb_model_id)
    assert read == [(LANGUAGE_MODELS, KB_MODEL_SETTING)]


def test_the_function_reads_the_agent_model_from_the_agent_setting(strands_module, monkeypatch):
    """The two must not be crossed: one setting runs the agent, the other the retrieval.

    Swapping them would leave the knowledge bases working and cap the agent at the
    knowledge base's narrower model list, which is the opposite of the point.
    """
    read = _settings_read(strands_module, monkeypatch, strands_module.configured_agent_model_id)
    assert read == [(LANGUAGE_MODELS, AGENT_MODEL_SETTING)]


# ── the function itself ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def strands_stub():
    """A stand-in for the `strands` package, which ships in a Lambda layer.

    Provides the two entry points this module reaches for: the `tool` decorator
    (the real one returns a callable wrapper, so returning the function itself
    keeps a tool body directly callable) and `strands.models`, whose `BedrockModel`
    the handler constructs. `Agent` is left for the handler test to supply.

    The object registered in sys.modules is the one returned, so that a second
    test file standing `strands` in does not end up patching a different object
    from the one `from strands import Agent` resolves to. That shared object is
    why the registration reuses an existing `strands` entry rather than replacing
    it, and it means these fixtures would take over a real `strands` if the SDK
    were installed here. That is intended: these tests call tool bodies directly,
    which needs `tool` to be the identity function rather than the SDK's wrapper,
    so the stub is wanted whether or not the package is present. The suites are
    the no-AWS static job, which installs no Lambda-layer packages.
    """
    stub = sys.modules.setdefault("strands", types.ModuleType("strands"))
    stub.tool = lambda func: func
    stub.models = sys.modules.setdefault("strands.models", types.ModuleType("strands.models"))
    return stub


@pytest.fixture(scope="module")
def strands_module(strands_stub):
    """The meeting-assist Lambda's module, imported with `strands` stood in for.

    Loaded by path under a name of its own, with its own directory on sys.path so
    that its sibling imports (`bedrock_compat`, `asst_gql_client`) resolve. A
    region is set because the module creates boto3 clients at import; no AWS call
    is made by any test here.
    """
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_REGION", "us-east-1")

    sys.path.insert(0, str(FUNCTION_SRC))
    if str(CONFIGURATION_LAYER) not in sys.path:
        sys.path.append(str(CONFIGURATION_LAYER))
    try:
        spec = importlib.util.spec_from_file_location(
            "lma_strands_meeting_assist",
            FUNCTION_SRC / "strands_meeting_assist_function.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(FUNCTION_SRC))
    return module


@pytest.mark.parametrize("model_id", KB_MODELS_VERIFIED_WORKING)
def test_a_verified_working_model_is_reported_as_usable_by_a_knowledge_base(
    strands_module, model_id: str
):
    assert strands_module.kb_model_supports_retrieve_and_generate(model_id) is True


@pytest.mark.parametrize("model_id", KB_MODELS_VERIFIED_REFUSED)
def test_a_verified_refused_model_is_reported_as_needing_custom_prompts(
    strands_module, model_id: str
):
    """Including Claude Opus 4.1, which no generation rule would catch.

    It is Claude 4, so the function's generation default treats it as usable; it
    is refused all the same, and only the recorded list of known refusals makes
    the function agree with the knowledge base about it.
    """
    assert strands_module.kb_model_supports_retrieve_and_generate(model_id) is False


def test_the_unmeasured_claude_5_5_id_is_withheld_by_the_generation_rule(strands_module):
    """`us.anthropic.claude-opus-5-5` is reported unusable without a measurement.

    Five of the six Claude 5.5 ids were called against a live knowledge base and
    refused; this one was not called, so it is deliberately absent from
    KB_MODELS_VERIFIED_REFUSED. The generation default is what withholds it, and
    this pins that -- so the docstrings and the CHANGELOG can say the id is
    covered by a rule rather than by a result, and stay true if the recorded
    refusals change.
    """
    model_id = "us.anthropic.claude-opus-5-5"
    assert model_id not in KB_MODELS_VERIFIED_REFUSED, (
        f"{model_id} is in the measured-refusal record, so the wording that calls it "
        "unmeasured is now wrong"
    )
    assert strands_module.kb_model_supports_retrieve_and_generate(model_id) is False


def test_a_non_claude_model_keeps_the_knowledge_bases_default_prompts(strands_module):
    """Only the Claude generations are known to carry the requirement.

    A Nova id is not offered by the knowledge-base setting, but the guard is a
    statement about models, not about the list, and reporting it unusable would
    remove a tool for a model nothing has measured as failing.
    """
    assert strands_module.kb_model_supports_retrieve_and_generate("amazon.nova-pro-v1:0") is True


def test_a_bare_foundation_model_id_is_addressed_without_an_account(strands_module):
    arn = strands_module.build_kb_model_arn(
        "anthropic.claude-haiku-4-5-20251001-v1:0", "us-west-2", "123456789012"
    )
    assert arn == (
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_an_inference_profile_id_is_addressed_in_the_deployments_account(strands_module):
    arn = strands_module.build_kb_model_arn(
        "global.anthropic.claude-haiku-4-5-20251001-v1:0", "us-west-2", "123456789012"
    )
    assert arn == (
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/"
        "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def _kb_call(strands_module, monkeypatch) -> MagicMock:
    """Stand in for the bedrock-agent-runtime client and capture the request."""
    client = MagicMock()
    client.retrieve_and_generate.return_value = {"output": {"text": "an answer"}}
    monkeypatch.setattr(strands_module, "bedrock_agent_runtime", client)
    monkeypatch.setattr(strands_module, "APPSYNC_GRAPHQL_URL", "")
    return client


def test_meeting_history_generates_with_the_knowledge_base_model(strands_module, monkeypatch):
    """Not the agent's model, which may be one RetrieveAndGenerate rejects."""
    client = _kb_call(strands_module, monkeypatch)
    tool = strands_module.create_meeting_history_tool(
        transcript_kb_id="ABCDEFGHIJ",
        kb_region="us-west-2",
        kb_account_id="123456789012",
        kb_model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        user_email="someone@example.com",
    )
    assert tool("what did we decide") == "an answer"

    sent = client.retrieve_and_generate.call_args.kwargs
    model_arn = sent["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"]["modelArn"]
    assert model_arn.endswith("/global.anthropic.claude-haiku-4-5-20251001-v1:0")


def test_document_search_generates_with_the_knowledge_base_model(strands_module, monkeypatch):
    client = _kb_call(strands_module, monkeypatch)
    tool = strands_module.create_document_search_tool(
        kb_id="ABCDEFGHIJ",
        kb_region="us-west-2",
        kb_account_id="123456789012",
        kb_model_id="global.anthropic.claude-sonnet-4-6",
    )
    assert tool("what is the travel policy") == "an answer"

    sent = client.retrieve_and_generate.call_args.kwargs
    model_arn = sent["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"]["modelArn"]
    assert model_arn.endswith("/global.anthropic.claude-sonnet-4-6")


@pytest.mark.parametrize("model_id", KB_MODELS_VERIFIED_REFUSED)
def test_meeting_history_is_not_offered_when_the_knowledge_base_cannot_generate(
    strands_module, monkeypatch, model_id: str
):
    """No tool at all, rather than one the agent calls and cannot get an answer from."""
    _kb_call(strands_module, monkeypatch)
    assert (
        strands_module.create_meeting_history_tool(
            transcript_kb_id="ABCDEFGHIJ",
            kb_region="us-west-2",
            kb_account_id="123456789012",
            kb_model_id=model_id,
            user_email="someone@example.com",
        )
        is None
    )


@pytest.mark.parametrize("model_id", KB_MODELS_VERIFIED_REFUSED)
def test_document_search_is_not_offered_when_the_knowledge_base_cannot_generate(
    strands_module, monkeypatch, model_id: str
):
    _kb_call(strands_module, monkeypatch)
    assert (
        strands_module.create_document_search_tool(
            kb_id="ABCDEFGHIJ",
            kb_region="us-west-2",
            kb_account_id="123456789012",
            kb_model_id=model_id,
        )
        is None
    )


def test_the_unusable_model_is_named_in_the_log_when_a_tool_is_withheld(
    strands_module, monkeypatch, caplog
):
    """Whoever reads the log needs the id and the parameter, not just the absence."""
    _kb_call(strands_module, monkeypatch)
    with caplog.at_level("ERROR"):
        strands_module.create_meeting_history_tool(
            transcript_kb_id="ABCDEFGHIJ",
            kb_region="us-west-2",
            kb_account_id="123456789012",
            kb_model_id="global.anthropic.claude-sonnet-5-5",
            user_email="someone@example.com",
        )
    logged = caplog.text
    assert "global.anthropic.claude-sonnet-5-5" in logged
    assert "Meeting assistant knowledge bases" in logged


def test_a_failed_meeting_history_search_does_not_read_as_a_search_result(
    strands_module, monkeypatch, caplog
):
    """A failure the agent summarises is worse than no tool: it answers from it.

    The returned text has to say plainly that nothing was searched, and the log
    has to name the model the call was made with, since that is what distinguishes
    a model the knowledge base refuses from a throttled or unauthorised call.
    """
    client = _kb_call(strands_module, monkeypatch)
    client.retrieve_and_generate.side_effect = RuntimeError("ValidationException: nope")

    tool = strands_module.create_meeting_history_tool(
        transcript_kb_id="ABCDEFGHIJ",
        kb_region="us-west-2",
        kb_account_id="123456789012",
        kb_model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        user_email="someone@example.com",
    )
    with caplog.at_level("ERROR"):
        result = tool("what did we decide")

    assert result.startswith("MEETING_HISTORY_SEARCH_FAILED")
    assert "No past meetings were searched" in result
    assert "global.anthropic.claude-haiku-4-5-20251001-v1:0" in caplog.text


def test_a_failed_document_search_does_not_read_as_a_search_result(
    strands_module, monkeypatch, caplog
):
    client = _kb_call(strands_module, monkeypatch)
    client.retrieve_and_generate.side_effect = RuntimeError("AccessDeniedException: nope")

    tool = strands_module.create_document_search_tool(
        kb_id="ABCDEFGHIJ",
        kb_region="us-west-2",
        kb_account_id="123456789012",
        kb_model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    with caplog.at_level("ERROR"):
        result = tool("what is the travel policy")

    assert result.startswith("DOCUMENT_SEARCH_FAILED")
    assert "No documents were retrieved" in result
    assert "global.anthropic.claude-haiku-4-5-20251001-v1:0" in caplog.text


# ── the handler, where the two models are handed out ────────────────────────

AGENT_MODEL_UNDER_TEST = "global.anthropic.claude-sonnet-5-5"
KB_MODEL_UNDER_TEST = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_the_handler_gives_the_knowledge_base_tools_the_knowledge_base_model(
    strands_module, strands_stub, monkeypatch
):
    """The agent on a Claude 5.5 model, retrieval on Claude 4.5, in one invocation.

    This is the arrangement the separate setting exists to allow, and the
    handler is the only place the two are told apart, so it is asserted here
    rather than inferred from the factories accepting a `kb_model_id` argument.
    The Strands agent is stood in for: `Agent` records the tools it was given and
    answers without Bedrock, and `BedrockModel` records the model it was asked
    for.
    """
    monkeypatch.setattr(strands_module, "configured_agent_model_id", lambda: AGENT_MODEL_UNDER_TEST)
    monkeypatch.setattr(strands_module, "configured_kb_model_id", lambda: KB_MODEL_UNDER_TEST)
    monkeypatch.setenv("KB_ID", "JIHGFEDCBA")
    monkeypatch.setenv("TRANSCRIPT_KB_ID", "ABCDEFGHIJ")
    monkeypatch.setenv("KB_REGION", "us-west-2")
    monkeypatch.setenv("KB_ACCOUNT_ID", "123456789012")
    # No MCP servers, no AppSync and no DynamoDB table, so this invocation needs
    # nothing but the stubs below.
    monkeypatch.setattr(strands_module, "MCP_LOADER_AVAILABLE", False)
    monkeypatch.setattr(strands_module, "APPSYNC_GRAPHQL_URL", "")
    monkeypatch.setattr(strands_module, "EVENT_API_HTTP_URL", "")
    _kb_call(strands_module, monkeypatch)

    factory_calls: dict[str, dict] = {}

    def record(name, factory):
        def wrapper(**kwargs):
            factory_calls[name] = kwargs
            return factory(**kwargs)

        return wrapper

    monkeypatch.setattr(
        strands_module,
        "create_meeting_history_tool",
        record("meeting_history", strands_module.create_meeting_history_tool),
    )
    monkeypatch.setattr(
        strands_module,
        "create_document_search_tool",
        record("document_search", strands_module.create_document_search_tool),
    )

    bedrock_model_kwargs: dict = {}

    class StubBedrockModel:
        def __init__(self, **kwargs):
            bedrock_model_kwargs.update(kwargs)

    agent_tools: list = []

    class StubAgent:
        def __init__(self, model=None, system_prompt=None, tools=None, hooks=None):
            agent_tools.extend(tools or [])

        def __call__(self, _message):
            return "a stubbed answer"

    monkeypatch.setattr(strands_stub, "Agent", StubAgent, raising=False)
    monkeypatch.setattr(strands_stub.models, "BedrockModel", StubBedrockModel, raising=False)

    result = strands_module.handler(
        {
            "userInput": "what did we decide last week",
            "callId": "call-under-test",
            "userEmail": "someone@example.com",
        },
        None,
    )

    assert result["message"] == "a stubbed answer", f"handler did not complete: {result}"
    assert factory_calls["meeting_history"]["kb_model_id"] == KB_MODEL_UNDER_TEST
    assert factory_calls["document_search"]["kb_model_id"] == KB_MODEL_UNDER_TEST
    assert bedrock_model_kwargs["model_id"] == AGENT_MODEL_UNDER_TEST
    # Both knowledge-base tools are offered, and both generate with the
    # knowledge-base model, even though the agent runs on a model a knowledge base
    # refuses. That is the whole point of the two settings being independent.
    #
    # Asserted by name rather than by count so that the absence of web_search is a
    # stated condition of this test -- this deployment configures no Tavily secret
    # -- rather than something a reader has to infer from the number 2.
    assert sorted(t.__name__ for t in agent_tools) == ["document_search", "meeting_history"]
