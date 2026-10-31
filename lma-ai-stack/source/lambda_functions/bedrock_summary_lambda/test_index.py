# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the meeting-summary Lambda that prompts Amazon Bedrock.

What this function produces is the artifact meeting owners read after the call and
the artifact the knowledge base is built from, so the failures that matter are the
silent ones: a prompt assembled with the transcript missing, an inference
configuration that quietly changes answer quality, a template that should have been
skipped being sent anyway, or a Bedrock error turning into a plausible-looking
summary. Every assertion below pins an exact request or an exact stored value
rather than only checking that a field exists.

Bedrock is never called: the module-level ``bedrock`` client is replaced with a
recorder, as are the Lambda, DynamoDB and S3 clients. The one thing read from the
real client is its retry configuration, which is a construction-time setting and
needs no call.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

MODEL_ID = "us.amazon.nova-pro-v1:0"
FETCH_ARN = "arn:aws:lambda:us-east-1:123456789012:function:fetch-transcript"
TABLE_NAME = "lma-configuration"
BUCKET = "lma-meeting-bucket"
PREFIX = "kb/"

# index.py reads all of these at import time and builds three boto3 clients, so
# they have to be in place before the module is loaded. Client construction
# resolves an endpoint, which needs a region even though no call is ever made.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("BEDROCK_MODEL_ID", MODEL_ID)
os.environ.setdefault("FETCH_TRANSCRIPT_LAMBDA_ARN", FETCH_ARN)
os.environ.setdefault("S3_BUCKET_NAME", BUCKET)
os.environ.setdefault("S3_PREFIX", PREFIX)
os.environ.setdefault("CONFIGURATION_TABLE_NAME", TABLE_NAME)
# PROCESS_TRANSCRIPT is deliberately left unset: its default
# are asserted below.

HERE = Path(__file__).resolve().parent
# source/lambda_functions/bedrock_summary_lambda -> source
LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from lma_config import codec  # noqa: E402
from lma_config.keys import ACTIVE_POINTER_KEY, DEFAULT_KEY  # noqa: E402
from lma_config.store import ConfigurationStore  # noqa: E402


def _load_module():
    """Import this directory's index.py under a name of its own.

    Every Lambda source directory in this tree has an ``index.py``, so a plain
    ``import index`` resolves to whichever directory happens to come first on
    sys.path.
    """
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("bedrock_summary_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load_module()

CALL_ID = "8cfc6ec4-0dbe-4959-b1f3-34f13359826b"
# Object names are sanitised before use, and a hyphen is not in the permitted set,
# so a call id's hyphens appear as underscores in the stored keys.
SAFE_CALL_ID = CALL_ID.replace("-", "_")
TRANSCRIPT = "Bob: we ship on Friday\nAlice: agreed"

METADATA = {
    "CallId": CALL_ID,
    "CreatedAt": "2025-01-02T03:04:05.000Z",
    "UpdatedAt": "2025-01-02T03:44:05.000Z",
    "Owner": "bob@example.com",
    "TotalConversationDurationMillis": 125400,
    # Present on the real item and not indexed by the knowledge base.
    "PK": f"c#{CALL_ID}",
    "AgentId": "agent-7",
}


class FakeBedrock:
    """Records every converse() request and returns canned model output."""

    def __init__(self, texts=None, error=None, response=None):
        self.requests = []
        self.texts = list(texts) if texts is not None else []
        self.error = error
        self.response = response

    def converse(self, **kwargs):
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        text = self.texts.pop(0) if self.texts else "a summary"
        return {"output": {"message": {"content": [{"text": text}]}}}


class FakeLambdaClient:
    """Stands in for the fetch-transcript invocation."""

    def __init__(self, transcript=TRANSCRIPT, metadata=None):
        self.requests = []
        self.payload = {
            "transcript": transcript,
            "metadata": dict(METADATA) if metadata is None else metadata,
        }

    def invoke(self, **kwargs):
        self.requests.append(kwargs)
        # The real client hands back a streaming body, which index.py read()s.
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode())}


class FakeConfigurationTable:
    """The shared configuration table, holding a baseline and one profile.

    Only `get_item` is needed: this function reads configuration and never
    writes it. Its role stays read-only across the migration -- `GetItem` alone,
    now on the configuration table instead of the previous per-store one.
    """

    SECTION = "summary_prompt_templates"

    def __init__(self, default=None, custom=None):
        self.items = {}
        if default is not None:
            self.items[DEFAULT_KEY] = self._record(DEFAULT_KEY, default)
        if custom is not None:
            self.items["Config#custom"] = self._record("Config#custom", custom)
            self.items[ACTIVE_POINTER_KEY] = {
                "Configuration": {"S": ACTIVE_POINTER_KEY},
                "ActiveProfile": {"S": "custom"},
            }
        self.requests = []

    @classmethod
    def _record(cls, key, values):
        return {
            "Configuration": {"S": key},
            "Version": {"N": "1"},
            "Body": {"B": codec.encode_body({cls.SECTION: values})},
        }

    def get_item(self, **kwargs):
        self.requests.append(kwargs)
        key = kwargs["Key"]["Configuration"]["S"]
        item = self.items.get(key)
        # A real get_item omits Item entirely when the key is absent.
        return {"Item": item} if item is not None else {}


class FakeS3Client:
    """Records the objects the knowledge-base export would have written."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):  # noqa: N803 - boto3 kwarg names
        self.objects[Key] = {"Bucket": Bucket, "Body": Body}


def ddb_item(**pairs):
    """The stored contents of one configuration record's prompt section."""
    return dict(pairs)


def install(monkeypatch, bedrock=None, templates=None, custom=None, lambda_client=None):
    """Replace every AWS client the module built at import time."""
    bedrock = bedrock if bedrock is not None else FakeBedrock()
    s3 = FakeS3Client()
    monkeypatch.setattr(index, "bedrock", bedrock)
    table = FakeConfigurationTable(templates, custom)
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore(TABLE_NAME, client=table))
    monkeypatch.setattr(
        index, "lambda_client", lambda_client if lambda_client is not None else FakeLambdaClient()
    )
    # write_to_s3 creates its client on each call, so patch the factory.
    monkeypatch.setattr(index.boto3, "client", lambda *_a, **_kw: s3)
    return bedrock, s3


def prompts_sent(bedrock):
    """The prompt text of each request, in the order Bedrock received them."""
    return [request["messages"][0]["content"][0]["text"] for request in bedrock.requests]


# --------------------------------------------------------------------------
# Prompt construction from the stored template
# --------------------------------------------------------------------------


def test_transcript_replaces_the_placeholder_in_the_stored_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bedrock, _ = install(monkeypatch)
    index.generate_summary(TRANSCRIPT, "Summarize this meeting:\n{transcript}\nBe brief.")
    assert prompts_sent(bedrock) == [f"Summarize this meeting:\n{TRANSCRIPT}\nBe brief."]


def test_an_empty_transcript_leaves_no_placeholder_text_in_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A meeting with no final segments must not send the literal placeholder.

    Sending "{transcript}" to the model produces a confident summary of nothing,
    which is worse than an empty one because it reads as real.
    """
    bedrock, _ = install(monkeypatch)
    index.generate_summary("", "Summarize:\n{transcript}\nEND")
    assert prompts_sent(bedrock) == ["Summarize:\n\nEND"]
    assert "{transcript}" not in prompts_sent(bedrock)[0]


def test_a_template_that_names_no_placeholder_is_sent_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only {transcript} is substituted; other braces are literal prompt text."""
    bedrock, _ = install(monkeypatch)
    index.generate_summary(TRANSCRIPT, "List the action items. Use {json} format.")
    assert prompts_sent(bedrock) == ["List the action items. Use {json} format."]


def test_every_occurrence_of_the_placeholder_is_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bedrock, _ = install(monkeypatch)
    index.generate_summary("T", "{transcript} then {transcript}")
    assert prompts_sent(bedrock) == ["T then T"]


def test_line_breaks_stored_as_br_tags_are_sent_as_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt editor in the UI stores newlines as <br>, one line per tag."""
    bedrock, _ = install(monkeypatch)
    index.generate_summary("T", "line one<br>line two<br>{transcript}")
    assert prompts_sent(bedrock) == ["line one\nline two\nT"]


def test_a_json_override_sends_one_prompt_per_named_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-section override keeps its section names and its line breaks."""
    bedrock, _ = install(monkeypatch, bedrock=FakeBedrock(texts=["one line", "two items"]))
    result = index.generate_summary(
        TRANSCRIPT,
        json.dumps(
            {"Summary": "Summarize:<br>{transcript}", "Action Items": "Actions: {transcript}"}
        ),
    )
    assert prompts_sent(bedrock) == [
        f"Summarize:\n{TRANSCRIPT}",
        f"Actions: {TRANSCRIPT}",
    ]
    assert json.loads(result) == {"Summary": "one line", "Action Items": "two items"}


def test_a_plain_text_override_returns_the_model_text_without_wrapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One template means the caller gets the model's answer verbatim.

    The chat UI renders this string directly, so wrapping it in a JSON object
    would surface as braces and quotes in the meeting pane.
    """
    _, _ = install(monkeypatch, bedrock=FakeBedrock(texts=["They agreed to ship Friday."]))
    result = index.generate_summary(TRANSCRIPT, "What did they agree? {transcript}")
    assert result == "They agreed to ship Friday."


# --------------------------------------------------------------------------
# Templates read from DynamoDB
# --------------------------------------------------------------------------


def test_templates_are_sent_in_stored_sort_order_with_the_sort_prefix_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The numeric prefix orders the sections and must not reach the output keys.

    Section order is what the reader sees, and the prefix is an ordering device
    only, so "1#Summary" has to arrive as "Summary".
    """
    bedrock, _ = install(
        monkeypatch,
        bedrock=FakeBedrock(texts=["first", "second", "third"]),
        templates=ddb_item(**{"2#Topics": "topics {transcript}", "1#Summary": "sum {transcript}"}),
        custom=ddb_item(**{"3#Actions": "acts {transcript}"}),
    )
    result = json.loads(index.generate_summary("T", None))
    assert list(result.keys()) == ["Summary", "Topics", "Actions"]
    assert prompts_sent(bedrock) == ["sum T", "topics T", "acts T"]


def test_a_custom_template_replaces_the_default_of_the_same_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin edits are stored as a separate item and must win over the shipped one."""
    bedrock, _ = install(
        monkeypatch,
        templates=ddb_item(**{"1#Summary": "the default prompt"}),
        custom=ddb_item(**{"1#Summary": "the admin's prompt"}),
    )
    index.generate_summary("T", None)
    assert prompts_sent(bedrock) == ["the admin's prompt"]


def test_templates_left_empty_or_set_to_none_are_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NONE is how an admin turns a shipped section off, and it costs a call.

    An empty or disabled section that still reached Bedrock would bill for a
    request and add a blank heading to the summary.
    """
    bedrock, _ = install(
        monkeypatch,
        templates=ddb_item(
            **{"1#Summary": "keep me", "2#Topics": "NONE", "3#Actions": ""},
        ),
        custom=ddb_item(),
    )
    result = index.generate_summary("T", None)
    assert prompts_sent(bedrock) == ["keep me"]
    assert result == "a summary"


def test_only_keys_in_the_sequence_form_are_treated_as_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt key is 'N#Title'; every other attribute is bookkeeping.

    The templates share their item with the partition key, the *Information*
    text and the attributes recording who last saved the record, and more may be
    added later. Reading by the 'N#Title' form rather than by excluding the names
    known today keeps an added attribute from being sent to Bedrock as a prompt
    and appearing as a section of the summary. The form is the contract the
    seeded *Information* text states.
    """
    bedrock, _ = install(
        monkeypatch,
        templates=ddb_item(**{"*Information*": "edit these in the UI", "1#Summary": "real prompt"}),
        custom=ddb_item(
            **{
                "UpdatedBy": "admin@example.com",
                "UpdatedAt": "2026-10-22T18:30:00.000000Z",
                "Owner": "someone",
            }
        ),
    )
    index.generate_summary("T", None)
    assert prompts_sent(bedrock) == ["real prompt"]


def test_a_key_that_is_not_a_prompt_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Selecting by the 'N#Title' form is narrower than the reader used to be.

    A key with no sequence number was previously used as a prompt, titled with
    the whole key. Only a hand edit in DynamoDB can store one -- the UI writes
    'N#Title' and nothing else -- so the key is skipped, but it is reported
    rather than dropped silently, since whoever added it is the person who needs
    to know it is not in use.
    """
    bedrock, _ = install(
        monkeypatch,
        templates=ddb_item(**{"1#Summary": "real prompt"}),
        custom=ddb_item(**{"MyHandAddedSection": "please summarize the risks"}),
    )
    index.generate_summary("T", None)

    assert prompts_sent(bedrock) == ["real prompt"]
    logged = capsys.readouterr().out
    assert "MyHandAddedSection" in logged
    assert "N#Title" in logged


def test_a_records_provenance_never_reaches_the_prompt_section(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Who saved a record is a metadata attribute of the item, not a prompt.

    The configuration accessor keeps the metadata attributes out of the body, so
    `UpdatedBy` and `UpdatedAt` cannot arrive in the prompt section -- which is
    what lets this function report *every* key that is not a prompt without
    putting a line in each summary's log and training an operator to ignore the
    one that matters.
    """
    table = FakeConfigurationTable(ddb_item(**{"1#Summary": "real prompt"}), ddb_item())
    table.items[DEFAULT_KEY]["UpdatedBy"] = {"S": "admin@example.com"}
    table.items[DEFAULT_KEY]["UpdatedAt"] = {"S": "2026-10-22T18:30:00.000000Z"}
    bedrock = FakeBedrock()
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore(TABLE_NAME, client=table))
    monkeypatch.setattr(index, "bedrock", bedrock)
    monkeypatch.setattr(index.boto3, "client", lambda *_a, **_kw: FakeS3Client())

    index.generate_summary("T", None)

    # Both halves: the prompt was sent, and the metadata attributes beside it
    # produced no line of their own. A test asserting only the quiet log would
    # pass against a reader that sent nothing at all.
    assert prompts_sent(bedrock) == ["real prompt"]
    assert "Not using" not in capsys.readouterr().out


def test_an_end_of_meeting_run_is_served_from_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unattended path is what the section's cache tier is for."""
    table = FakeConfigurationTable(ddb_item(**{"1#Summary": "p {transcript}"}), ddb_item())
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore(TABLE_NAME, client=table))
    monkeypatch.setattr(index, "bedrock", FakeBedrock())

    index.get_templates_from_dynamodb(None)
    reads = len(table.requests)
    index.get_templates_from_dynamodb(None)
    assert len(table.requests) == reads


def test_a_regenerate_run_reads_the_prompts_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Meeting Summary panel promises the prompts "as they are now".

    An administrator who saves a prompt and presses Regenerate within the
    section's cache window would otherwise get a summary from the prompt they
    just replaced, with nothing on the page saying so. The regenerator's invoke
    asks for a re-read, and this is the assertion that the request is honoured.
    """
    table = FakeConfigurationTable(
        ddb_item(**{"1#Summary": "the old prompt {transcript}"}), ddb_item()
    )
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore(TABLE_NAME, client=table))
    monkeypatch.setattr(index, "bedrock", FakeBedrock())

    assert index.get_templates_from_dynamodb(None) == [{"Summary": "the old prompt {transcript}"}]

    table.items[DEFAULT_KEY]["Body"] = {
        "B": codec.encode_body({FakeConfigurationTable.SECTION: {"1#Summary": "the new prompt"}})
    }
    assert index.get_templates_from_dynamodb(None, refresh_configuration=True) == [
        {"Summary": "the new prompt"}
    ]


def test_the_handler_asks_for_a_re_read_only_when_the_invoke_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag reaches the read, and an end-of-meeting invoke does not set it."""
    seen = []
    monkeypatch.setattr(
        index,
        "generate_summary",
        lambda transcript, override, refresh=False, call_id=None, agenda=None: (
            seen.append(refresh) or "a summary"
        ),
    )
    install(monkeypatch, templates=ddb_item(**{"1#Summary": "p"}), custom=ddb_item())
    index.handler({"CallId": CALL_ID}, None)
    index.handler({"CallId": CALL_ID, "RefreshConfiguration": True}, None)
    assert seen == [False, True]


def test_templates_are_read_from_the_shared_configuration_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keys are the shared table's, not the retired per-store table's.

    Reading the wrong key reads nothing and generates a summary with no prompts
    at all, which is a silent failure, so the requests are asserted exactly.
    """
    fake = FakeConfigurationTable(ddb_item(**{"1#Summary": "p"}), ddb_item())
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore(TABLE_NAME, client=fake))
    monkeypatch.setattr(index, "bedrock", FakeBedrock())
    index.get_templates_from_dynamodb(None)
    assert {request["TableName"] for request in fake.requests} == {TABLE_NAME}
    assert [request["Key"]["Configuration"]["S"] for request in fake.requests] == [
        "Config#__active",
        "Config#default",
        "Config#custom",
    ]


def test_a_warm_container_does_not_reread_the_prompts_for_every_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the section's declared standard cache tier is for.

    Before the shared accessor this function issued two fresh get_item calls per
    summary. The prompts are now read at most once per five minutes per
    container, and the same prompts are still used in between.
    """
    fake = FakeConfigurationTable(ddb_item(**{"1#Summary": "p {transcript}"}), ddb_item())
    monkeypatch.setattr(index, "CONFIG_STORE", ConfigurationStore(TABLE_NAME, client=fake))
    monkeypatch.setattr(index, "bedrock", FakeBedrock())

    assert index.get_templates_from_dynamodb(None) == [{"Summary": "p {transcript}"}]
    reads = len(fake.requests)
    assert reads > 0
    assert index.get_templates_from_dynamodb(None) == [{"Summary": "p {transcript}"}]
    assert len(fake.requests) == reads


def test_a_prompt_that_is_not_text_is_reported_rather_than_sent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Only a hand edit of the stored record can produce one."""
    bedrock, _ = install(
        monkeypatch,
        templates=ddb_item(**{"1#Summary": "real prompt"}),
        custom={"2#Topics": {"nested": "object"}},
    )
    index.generate_summary("T", None)
    assert prompts_sent(bedrock) == ["real prompt"]
    assert "2#Topics" in capsys.readouterr().out


# --------------------------------------------------------------------------
# What is sent to Bedrock
# --------------------------------------------------------------------------


def test_the_request_names_the_configured_model_and_a_deterministic_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model id and inference configuration change answers without erroring.

    temperature 0 is what makes two summaries of the same meeting agree, and
    maxTokens is the ceiling on summary length; both are silent if wrong. The
    configured model here is a Nova one, which takes a temperature, so this pins
    the shape sent to a model that accepts one -- the reasoning-capable Claude
    generations get a different shape, asserted below.
    """
    bedrock, _ = install(monkeypatch)
    index.call_bedrock("a prompt")
    assert bedrock.requests == [
        {
            "modelId": MODEL_ID,
            "messages": [{"role": "user", "content": [{"text": "a prompt"}]}],
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0},
        }
    ]


def test_no_temperature_is_sent_to_a_model_that_rejects_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude 5 and later refuse a request whose inferenceConfig carries temperature.

    Not a quality setting but a hard failure: every summary on a stack configured
    with one of those models would come back as the error string.
    """
    monkeypatch.setattr(index, "BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-5-5")
    bedrock, _ = install(monkeypatch)
    index.call_bedrock("a prompt")
    assert bedrock.requests[0]["inferenceConfig"] == {"maxTokens": 4096}


def test_the_budget_leaves_room_for_reasoning_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning is billed against maxTokens and is produced before any text.

    Measured on a real transcript, a reasoning model spent more than 2,048 output
    tokens on one summary and returned nothing at all under the 512 this used to
    send. A budget below the observed need produces an empty summary rather than
    an error, so the figure is pinned here.
    """
    bedrock, _ = install(monkeypatch)
    index.call_bedrock("a prompt")
    assert bedrock.requests[0]["inferenceConfig"]["maxTokens"] >= 2048


def test_the_bedrock_client_retries_throttled_requests_many_times() -> None:
    """Summaries run in bursts at meeting end, so throttling is routine.

    Adaptive mode with a high attempt ceiling is what keeps a throttled summary
    from being replaced by the error string. botocore reports the ceiling as
    total_max_attempts, one more than the configured retry count.
    """
    retries = index.bedrock.meta.config.retries
    assert retries["mode"] == "adaptive"
    assert retries["total_max_attempts"] == 51


def test_the_bedrock_client_is_built_for_the_regions_runtime_endpoint() -> None:
    """The endpoint must be the bedrock-*runtime* one for whichever region is set.

    Asserted as a relationship rather than against a literal region. The module
    resolves its region from the environment, and the test file can only
    `setdefault` it — so on a machine that already exports AWS_REGION (a
    developer's shell, or a CI runner with a region configured) a hard-coded
    region would fail for a reason that has nothing to do with the code.
    """
    assert index.BEDROCK_REGION, "no region resolved for the Bedrock client"
    assert index.bedrock.meta.endpoint_url == (
        f"https://bedrock-runtime.{index.BEDROCK_REGION}.amazonaws.com"
    )


# --------------------------------------------------------------------------
# Reading the model's response
# --------------------------------------------------------------------------


def test_the_summary_is_every_text_block_joined_in_order() -> None:
    """The API does not promise exactly one text block, so all of them are read."""
    response = {
        "output": {"message": {"content": [{"text": "the summary"}, {"text": " continued"}]}},
        "stopReason": "end_turn",
    }
    assert index.get_generated_text(response) == "the summary continued"


def test_a_reasoning_block_is_not_part_of_the_summary() -> None:
    """A reasoning-capable model puts its reasoning in a block before the text.

    Reading by position would store the model's working as the meeting summary,
    or raise, depending on which key that block happens to carry. Only text
    blocks are read.
    """
    response = {
        "output": {
            "message": {
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "let me think"}}},
                    {"text": "the summary"},
                ]
            }
        },
        "stopReason": "end_turn",
    }
    assert index.get_generated_text(response) == "the summary"


def test_a_single_text_block_still_reads_the_same_way() -> None:
    """Whether a reasoning block appears is prompt-dependent, not model-dependent.

    The same model answers a short prompt with one text block and a long
    transcript with reasoning first, so both shapes have to work.
    """
    response = {
        "output": {"message": {"content": [{"text": "the summary"}]}},
        "stopReason": "end_turn",
    }
    assert index.get_generated_text(response) == "the summary"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"output": {}},
        {"output": {"message": {"content": []}}},
        {"output": {"message": {"content": [{}]}}},
        {"output": {"message": {"content": "not a list"}}},
        {
            "output": {
                "message": {
                    "content": [{"reasoningContent": {"reasoningText": {"text": "thinking"}}}]
                }
            },
            "stopReason": "max_tokens",
        },
    ],
    ids=[
        "empty",
        "no-message",
        "no-blocks",
        "block-without-text",
        "blocks-not-a-list",
        "reasoning-only-budget-exhausted",
    ],
)
def test_a_response_of_an_unexpected_shape_is_rejected_rather_than_summarized(
    response,
) -> None:
    """No partial or coerced reading of a response the model did not produce.

    Anything other than the documented converse shape has to raise so the caller
    reports an error, rather than storing a fragment as the meeting summary. The
    last case is the one a reasoning model produces when the whole budget went on
    reasoning: there is no text to store, and a blank string overwriting a good
    summary is worse than a reported error.
    """
    with pytest.raises(ValueError):
        index.get_generated_text(response)


def test_an_unexpected_response_shape_is_reported_as_an_error_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, s3 = install(
        monkeypatch,
        bedrock=FakeBedrock(response={"output": {"message": {}}}),
        templates=ddb_item(**{"1#Summary": "p {transcript}"}),
        custom=ddb_item(),
    )
    assert index.handler({"CallId": CALL_ID}, None) == {"summary": "An error occurred."}
    assert s3.objects == {}


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


def throttling_error():
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}}, "Converse"
    )


def test_a_throttled_request_is_reported_as_an_error_and_nothing_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller gets a string, never an exception, and no partial export.

    A half-written knowledge-base object would be indexed and then answer
    questions from a meeting that has no summary.
    """
    _, s3 = install(
        monkeypatch,
        bedrock=FakeBedrock(error=throttling_error()),
        templates=ddb_item(**{"1#Summary": "p {transcript}"}),
        custom=ddb_item(),
    )
    assert index.handler({"CallId": CALL_ID}, None) == {"summary": "An error occurred."}
    assert s3.objects == {}


def test_a_throttled_request_propagates_out_of_the_bedrock_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call_bedrock does not swallow the error itself; botocore's retries do.

    Retry behaviour belongs to the client configuration, so a hand-rolled catch
    here would defeat adaptive rate limiting.
    """
    install(monkeypatch, bedrock=FakeBedrock(error=throttling_error()))
    with pytest.raises(ClientError):
        index.call_bedrock("a prompt")


def test_a_missing_template_item_is_raised_rather_than_summarized_from_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unseeded configuration table must not yield an empty prompt set.

    The accessor returns an empty section for a record that is not there, which
    is the right answer for a shared library -- a profile nobody has written is
    an ordinary state. It is this function that knows an empty prompt set is
    meaningless, so the refusal lives here.
    """
    install(monkeypatch, templates=None, custom=None)
    with pytest.raises(RuntimeError) as raised:
        index.get_templates_from_dynamodb(None)
    assert "seeder" in str(raised.value)


def test_a_configuration_table_failure_is_reported_as_an_error_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, templates=None, custom=None)
    assert index.handler({"CallId": CALL_ID}, None) == {"summary": "An error occurred."}


# --------------------------------------------------------------------------
# Fetching the transcript
# --------------------------------------------------------------------------


def test_the_transcript_request_names_the_call_and_asks_for_speaker_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speaker labels are what let the model attribute action items to people."""
    fake_lambda = FakeLambdaClient()
    install(
        monkeypatch,
        templates=ddb_item(**{"1#Summary": "p {transcript}"}),
        custom=ddb_item(),
        lambda_client=fake_lambda,
    )
    index.handler({"CallId": CALL_ID, "Prompt": "p {transcript}"}, None)
    assert len(fake_lambda.requests) == 1
    request = fake_lambda.requests[0]
    assert request["FunctionName"] == FETCH_ARN
    assert request["InvocationType"] == "RequestResponse"
    assert json.loads(request["Payload"]) == {
        "CallId": CALL_ID,
        "ProcessTranscript": False,
        "IncludeSpeaker": True,
    }


def test_the_whole_transcript_is_requested() -> None:
    """No word budget travels with the request any more.

    There used to be one, for models whose context window could not hold a
    meeting. It cut the end, which is where decisions land, and it was off in
    every shipped deployment. Asserted as an absence so it is not reinstated
    by reflex.
    """
    assert not hasattr(index, "TOKEN_COUNT")
    assert index.PROCESS_TRANSCRIPT is False


def test_the_transcript_request_carries_no_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_lambda = FakeLambdaClient()
    install(monkeypatch, lambda_client=fake_lambda)
    monkeypatch.setattr(index, "PROCESS_TRANSCRIPT", True)
    index.get_transcripts(CALL_ID)
    payload = json.loads(fake_lambda.requests[0]["Payload"])
    assert "TokenCount" not in payload
    assert payload["ProcessTranscript"] is True


def test_the_fetched_transcript_text_reaches_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: what the fetch function returns is what the model is asked about."""
    bedrock, _ = install(
        monkeypatch,
        templates=ddb_item(**{"1#Summary": "Summarize:<br>{transcript}"}),
        custom=ddb_item(),
        lambda_client=FakeLambdaClient(transcript="Alice: budget approved"),
    )
    index.handler({"CallId": CALL_ID}, None)
    assert prompts_sent(bedrock) == ["Summarize:\nAlice: budget approved"]


# --------------------------------------------------------------------------
# The knowledge-base export
# --------------------------------------------------------------------------


def two_section_setup(monkeypatch, texts=("one", "two")):
    return install(
        monkeypatch,
        bedrock=FakeBedrock(texts=list(texts)),
        templates=ddb_item(**{"1#Summary": "s {transcript}", "2#Topics": "t {transcript}"}),
        custom=ddb_item(),
    )


def test_the_summary_and_the_transcript_are_each_stored_with_a_metadata_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock knowledge bases read <object>.metadata.json beside each object.

    A missing or misnamed sidecar means the document is indexed without the
    attributes the UI filters meetings by.
    """
    _, s3 = two_section_setup(monkeypatch)
    index.handler({"CallId": CALL_ID}, None)
    assert sorted(s3.objects) == sorted(
        [
            f"{PREFIX}{SAFE_CALL_ID}-SUMMARY.txt",
            f"{PREFIX}{SAFE_CALL_ID}-SUMMARY.txt.metadata.json",
            f"{PREFIX}{SAFE_CALL_ID}-TRANSCRIPT.txt",
            f"{PREFIX}{SAFE_CALL_ID}-TRANSCRIPT.txt.metadata.json",
        ]
    )
    assert {obj["Bucket"] for obj in s3.objects.values()} == {BUCKET}
    assert s3.objects[f"{PREFIX}{SAFE_CALL_ID}-TRANSCRIPT.txt"]["Body"] == TRANSCRIPT


def test_the_stored_summary_carries_the_meeting_name_date_and_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These three fields are what a knowledge-base answer cites the meeting by."""
    _, s3 = two_section_setup(monkeypatch)
    index.handler({"CallId": CALL_ID}, None)
    stored = json.loads(s3.objects[f"{PREFIX}{SAFE_CALL_ID}-SUMMARY.txt"]["Body"])
    assert set(stored) == {
        "Summary",
        "Topics",
        "MEETING NAME",
        "MEETING DATE AND TIME",
        "MEETING DURATION (SECONDS)",
    }
    assert stored["Summary"] == "one"
    assert stored["Topics"] == "two"
    assert stored["MEETING NAME"] == CALL_ID
    assert stored["MEETING DATE AND TIME"] == "2025-01-02T03:04:05.000Z"
    # 125400 ms of conversation is 125 whole seconds, truncated not rounded.
    assert stored["MEETING DURATION (SECONDS)"] == 125


def test_the_recorded_duration_is_whole_seconds_truncated_from_milliseconds() -> None:
    summary = index.format_summary(
        "{}",
        {"CallId": "c", "CreatedAt": "t", "TotalConversationDurationMillis": 1999},
    )
    assert json.loads(summary)["MEETING DURATION (SECONDS)"] == 1


def test_only_the_indexed_attributes_are_written_to_the_metadata_sidecar() -> None:
    """Unlisted fields are dropped: the knowledge base rejects unknown attributes."""
    metadata = json.loads(index.getKBMetadata(METADATA))
    assert metadata == {
        "metadataAttributes": {
            "CallId": CALL_ID,
            "CreatedAt": "2025-01-02T03:04:05.000Z",
            "UpdatedAt": "2025-01-02T03:44:05.000Z",
            "Owner": "bob@example.com",
            "TotalConversationDurationMillis": 125400,
        }
    }


def test_metadata_attributes_that_the_call_lacks_are_simply_absent() -> None:
    """Meetings still in progress have no UpdatedAt; that is not an error."""
    metadata = json.loads(index.getKBMetadata({"CallId": CALL_ID}))
    assert metadata == {"metadataAttributes": {"CallId": CALL_ID}}


@pytest.mark.parametrize(
    ("call_id", "expected"),
    [
        ("meeting 2025/01/02", "meeting_2025_01_02"),
        ("__leading_and_trailing__", "leading_and_trailing"),
        ("Weekly Sync - Team A!", "Weekly_Sync___Team_A"),
        ("a.b_c9", "a.b_c9"),
    ],
    ids=["spaces-and-slashes", "underscore-edges", "punctuation", "already-safe"],
)
def test_object_names_keep_only_characters_that_are_safe_in_a_key(
    call_id: str, expected: str
) -> None:
    """Meeting names are user-supplied and become S3 keys and file names."""
    assert index.posixify_filename(call_id) == expected


def test_a_caller_supplied_prompt_result_is_returned_but_not_exported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the default templates feed the knowledge base.

    Ad-hoc prompts come from the chat pane; exporting their answers would
    overwrite the meeting's stored summary with a one-off reply.
    """
    _, s3 = install(monkeypatch, bedrock=FakeBedrock(texts=["an ad-hoc answer"]))
    result = index.handler({"CallId": CALL_ID, "Prompt": "What was decided? {transcript}"}, None)
    assert result == {"summary": "an ad-hoc answer"}
    assert s3.objects == {}


def test_the_handler_returns_the_summary_under_a_summary_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state machine and the AppSync resolver both read event["summary"]."""
    two_section_setup(monkeypatch)
    result = index.handler({"CallId": CALL_ID}, None)
    assert list(result) == ["summary"]
    assert json.loads(result["summary"])["Summary"] == "one"


def test_the_handler_reports_an_error_string_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers treat the return value as the summary, so failure must be in-band.

    Note that this also covers a failure to store the export: the summary that
    was generated is replaced by the error string if any later step raises.
    """

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("fetch transcript is unavailable")

    monkeypatch.setattr(index, "get_transcripts", unavailable)
    assert index.handler({"CallId": CALL_ID}, None) == {"summary": "An error occurred."}


def _root_level_after_import(monkeypatch: pytest.MonkeyPatch, log_level: str | None) -> int:
    """Import a fresh copy of index.py and return the root logger level it set.

    The root logger is process-wide, so its level is put back afterwards for the
    tests that follow.
    """
    import logging

    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)
    if log_level is None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LOG_LEVEL", log_level)
    spec = importlib.util.spec_from_file_location("bedrock_summary_index_level", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return root.level


@pytest.mark.parametrize(
    ("log_level", "expected"),
    [("DEBUG", 10), ("INFO", 20), ("WARN", 30), ("ERROR", 40)],
)
def test_root_logger_level_follows_the_stack_log_level(
    monkeypatch: pytest.MonkeyPatch, log_level: str, expected: int
) -> None:
    """Every value the stack's LogLevel parameter allows is honoured as given."""
    assert _root_level_after_import(monkeypatch, log_level) == expected


def test_root_logger_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without LOG_LEVEL, configuration-layer warnings are still emitted."""
    assert _root_level_after_import(monkeypatch, None) == 20


class FakeMeteringTable:
    def __init__(self):
        self.items = []

    def put_item(self, TableName, Item):  # noqa: N803
        self.items.append(Item)


def test_each_summary_section_records_its_token_usage_against_the_meeting(monkeypatch) -> None:
    """One record per model call, in the units the price table prices."""
    response = {
        "output": {"message": {"content": [{"text": "a summary"}]}},
        "usage": {"inputTokens": 1200, "outputTokens": 80, "totalTokens": 1280},
    }
    install(
        monkeypatch,
        bedrock=FakeBedrock(response=response),
        templates=ddb_item(**{"1#Summary": "p {transcript}", "2#Actions": "q {transcript}"}),
        custom=ddb_item(),
    )
    metering = FakeMeteringTable()
    monkeypatch.setattr(
        index, "METERING", index.MeteringWriter("calls", source="test", client=metering)
    )
    index.handler({"CallId": CALL_ID}, None)
    assert len(metering.items) == 2
    for item in metering.items:
        assert item["PK"] == {"S": f"mtr#{CALL_ID}"}
        assert item["Context"] == {"S": "Summary"}
        assert item["Service"] == {"S": f"bedrock/{index.BEDROCK_MODEL_ID}"}
        assert item["Usage"]["M"] == {
            "inputTokens": {"N": "1200"},
            "outputTokens": {"N": "80"},
            "requests": {"N": "1"},
        }


# --------------------------------------------------------------------------
# The agenda coverage section
# --------------------------------------------------------------------------

AGENDA_PROMPT = "Agenda:<br>{agenda}<br>Transcript:<br>{transcript}"


def test_a_prompt_naming_the_agenda_is_sent_with_it_numbered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bedrock, _ = install(
        monkeypatch,
        templates=ddb_item(**{"1#Summary": "S {transcript}", "2#Agenda Coverage": AGENDA_PROMPT}),
        custom=ddb_item(),
    )
    index.generate_summary("T", None, agenda="- Budget\n\n2) Hiring")
    assert prompts_sent(bedrock) == ["S T", "Agenda:\n1. Budget\n2. Hiring\nTranscript:\nT"]


def test_a_meeting_without_an_agenda_gets_no_agenda_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Left out, not generated empty: there is nothing to report against."""
    bedrock, _ = install(
        monkeypatch,
        bedrock=FakeBedrock(texts=["one", "two"]),
        templates=ddb_item(
            **{
                "1#Summary": "S {transcript}",
                "2#Agenda Coverage": AGENDA_PROMPT,
                "3#Actions": "A {transcript}",
            }
        ),
        custom=ddb_item(),
    )
    for agenda in (None, "", "  \n "):
        bedrock.requests.clear()
        result = json.loads(index.generate_summary("T", None, agenda=agenda))
        assert prompts_sent(bedrock) == ["S T", "A T"]
        assert "Agenda Coverage" not in result


def test_the_handler_passes_the_meetings_own_agenda(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(
        index,
        "generate_summary",
        lambda transcript, override, refresh=False, call_id=None, agenda=None: (
            seen.append(agenda) or "a summary"
        ),
    )
    install(
        monkeypatch,
        templates=ddb_item(**{"1#Summary": "p"}),
        custom=ddb_item(),
        lambda_client=FakeLambdaClient(metadata={**METADATA, "Agenda": "Budget"}),
    )
    index.handler({"CallId": CALL_ID}, None)
    assert seen == ["Budget"]


def test_the_shipped_agenda_section_names_the_agenda_and_the_transcript() -> None:
    shipped = json.loads(
        (
            HERE.parent / "configuration_seeder" / "defaults" / "LLMPromptSummaryTemplate.json"
        ).read_text(encoding="utf-8")
    )
    prompt = shipped["AGENDA COVERAGE"]
    assert "{agenda}" in prompt and "{transcript}" in prompt
    for status in ("Covered", "Partly covered", "Not covered"):
        assert status in prompt
    # Read by a summariser that predates the agenda, the placeholder is left as
    # it is; the prompt says what to answer then rather than inventing items.
    assert "No agenda was set for this meeting." in prompt


def test_the_agenda_is_split_on_line_breaks_only_as_the_page_does() -> None:
    assert (
        index.numbered_agenda("Budget\u2028review\r\nHiring") == "1. Budget\u2028review\n2. Hiring"
    )
    assert index.numbered_agenda(None) == ""
