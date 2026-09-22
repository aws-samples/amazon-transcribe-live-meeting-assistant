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
TABLE_NAME = "llm-prompt-templates"
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
os.environ.setdefault("LLM_PROMPT_TEMPLATE_TABLE_NAME", TABLE_NAME)
# PROCESS_TRANSCRIPT and TOKEN_COUNT are deliberately left unset: their defaults
# are asserted below.

HERE = Path(__file__).resolve().parent


def _load_module():
    """Import this directory's index.py under a name of its own.

    Every Lambda source directory in this tree has an ``index.py``, so a plain
    ``import index`` resolves to whichever directory happens to come first on
    sys.path.
    """
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


class FakeDynamoClient:
    """Serves the default and custom prompt-template items."""

    def __init__(self, default=None, custom=None):
        self.items = {
            index.DEFAULT_PROMPT_TEMPLATES_PK: default,
            index.CUSTOM_PROMPT_TEMPLATES_PK: custom,
        }
        self.requests = []

    def get_item(self, **kwargs):
        self.requests.append(kwargs)
        key = kwargs["Key"]["LLMPromptTemplateId"]["S"]
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
    """Build a DynamoDB item in wire form, as get_item returns it."""
    item = {"LLMPromptTemplateId": {"S": "ignored"}}
    item.update({key: {"S": value} for key, value in pairs.items()})
    return item


def install(monkeypatch, bedrock=None, templates=None, custom=None, lambda_client=None):
    """Replace every AWS client the module built at import time."""
    bedrock = bedrock if bedrock is not None else FakeBedrock()
    s3 = FakeS3Client()
    monkeypatch.setattr(index, "bedrock", bedrock)
    monkeypatch.setattr(index, "dynamodb_client", FakeDynamoClient(templates, custom))
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


def test_the_item_key_and_the_information_row_are_not_treated_as_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both are bookkeeping fields on the same item as the templates."""
    bedrock, _ = install(
        monkeypatch,
        templates=ddb_item(**{"*Information*": "edit these in the UI", "1#Summary": "real prompt"}),
        custom=ddb_item(),
    )
    index.generate_summary("T", None)
    assert prompts_sent(bedrock) == ["real prompt"]


def test_templates_are_read_from_the_configured_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDynamoClient(ddb_item(**{"1#Summary": "p"}), ddb_item())
    monkeypatch.setattr(index, "dynamodb_client", fake)
    monkeypatch.setattr(index, "bedrock", FakeBedrock())
    index.get_templates_from_dynamodb(None)
    assert [request["TableName"] for request in fake.requests] == [TABLE_NAME, TABLE_NAME]
    assert [request["Key"]["LLMPromptTemplateId"]["S"] for request in fake.requests] == [
        "DefaultSummaryPromptTemplates",
        "CustomSummaryPromptTemplates",
    ]


# --------------------------------------------------------------------------
# What is sent to Bedrock
# --------------------------------------------------------------------------


def test_the_request_names_the_configured_model_and_a_deterministic_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model id and inference configuration change answers without erroring.

    temperature 0 is what makes two summaries of the same meeting agree, and
    maxTokens is the ceiling on summary length; both are silent if wrong.
    """
    bedrock, _ = install(monkeypatch)
    index.call_bedrock("a prompt")
    assert bedrock.requests == [
        {
            "modelId": MODEL_ID,
            "messages": [{"role": "user", "content": [{"text": "a prompt"}]}],
            "inferenceConfig": {"maxTokens": 512, "temperature": 0},
        }
    ]


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


def test_the_summary_is_read_from_the_first_content_block() -> None:
    response = {
        "output": {"message": {"content": [{"text": "the summary"}, {"text": "trailing"}]}},
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
    ],
    ids=["empty", "no-message", "no-blocks", "block-without-text", "blocks-not-a-list"],
)
def test_a_response_of_an_unexpected_shape_is_rejected_rather_than_summarized(
    response,
) -> None:
    """No partial or coerced reading of a response the model did not produce.

    Anything other than the documented converse shape has to raise so the caller
    reports an error, rather than storing a fragment as the meeting summary.
    """
    with pytest.raises((KeyError, IndexError, TypeError)):
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
    """An unseeded template table must not yield an empty prompt set."""
    install(monkeypatch, templates=None, custom=None)
    with pytest.raises(KeyError):
        index.get_templates_from_dynamodb(None)


def test_a_template_table_failure_is_reported_as_an_error_summary(
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
        "TokenCount": 0,
        "IncludeSpeaker": True,
    }


def test_the_transcript_is_not_truncated_unless_a_token_count_is_configured() -> None:
    """Truncation drops the end of the meeting, where decisions usually land.

    Zero means "send it all"; the stack sets a non-zero count only for models
    with a small context window.
    """
    assert index.TOKEN_COUNT == 0
    assert index.PROCESS_TRANSCRIPT is False


def test_a_configured_token_count_is_passed_through_to_the_truncation_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncation happens in the fetch function, so the count must travel with it."""
    fake_lambda = FakeLambdaClient()
    install(monkeypatch, lambda_client=fake_lambda)
    monkeypatch.setattr(index, "TOKEN_COUNT", 12000)
    monkeypatch.setattr(index, "PROCESS_TRANSCRIPT", True)
    index.get_transcripts(CALL_ID)
    payload = json.loads(fake_lambda.requests[0]["Payload"])
    assert payload["TokenCount"] == 12000
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
