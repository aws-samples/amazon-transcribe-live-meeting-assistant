# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""How the Meeting Assistant publishes a streamed answer to AppSync (no AWS).

Two invariants, both about work done per token rather than per answer.

**One connection per answer.** gql's `Client.execute` opens a transport session,
runs the mutation and closes the session again, and for the requests transport
that session is where the TLS connection lives -- so a client built per token
costs a handshake per token. Measured against the deployed function's own logs,
that was 0.107s of overhead per streamed chunk. The tokens of one answer now go
out on one `AppsyncPublisher`, and a failed publish discards the session so the
next token reconnects instead of reusing one whose state is unknown.

**No publish where nothing is listening.** `onAddChatToken` requires both a
CallId and a MessageId, so only a caller that supplied the MessageId can be
subscribed. The Virtual Participant's voice assistant supplies none: it invokes
this function directly, waits for the whole response payload and speaks the
string it returns. Those invocations take the non-streaming path, and the answer
they get back is unchanged.

## What is stood in for, and why

This suite runs in the "no AWS" static job, whose environment is deliberately
thin: the LMA SDK's dev extra installs `gql` with none of its transport extras,
"the transports ... are not needed to build a query". Three things are therefore
replaced rather than imported.

* `strands`, which ships in a Lambda layer: a stand-in supplies the `tool`
  decorator, `models.BedrockModel` and `Agent`.
* `asst_gql_client`, the module that builds the real AppSync client. Importing it
  pulls in `gql.transport.requests`, which needs `requests_toolbelt` -- a package
  no requirements file in this repository declares, and one this suite has no use
  for, since it never makes an HTTP request. The stand-in is registered in
  `sys.modules` before the Lambda module is loaded, because the tool factories
  import it on the way to building a tool, before any seam is reachable.
* the AppSync client itself, through the module's own `new_appsync_client` seam,
  by the fake in `FakeClientFactory`.

A stand-in that is silently not used is worse than none, because an assertion
about what was *not* published passes for free. So every test that drives the
handler goes through `_invoke`, which fails with the handler's own return payload
if the handler did not complete -- a swallowed `ImportError` lands there -- and
asserts that streaming was configured in the first place; and the stub agent
records which entry point was used, so "answered without publishing" is asserted
as `stream_async` not being called, rather than as an empty list that an import
failure would also produce.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FUNCTION_SRC = REPO / "lma-meetingassist-setup-stack" / "src"

APPSYNC_URL = "https://example.test/graphql"

# The three payload shapes that reach this function, by caller. Only the first
# two carry an id a subscriber could be listening on.
CHAT_SEGMENT_ARGS = {"MessageId": "chat-0b7f", "SegmentId": "6d2a", "CallId": "call-1"}
WAKE_PHRASE_SEGMENT_ARGS = {"SegmentId": "6d2a", "CallId": "call-1"}
VOICE_ASSISTANT_SEGMENT_ARGS: dict = {}


# ── stand-ins ───────────────────────────────────────────────────────────────


class FakeSession:
    """What `Client.connect_sync()` hands back: a session that stays open."""

    def __init__(self, client: "FakeClient"):
        self.client = client

    def execute(self, document, variable_values=None):
        return self.client.record(variable_values, kind="session")


class FakeClient:
    """A stand-in for AppsyncRequestsGqlClient, counting connects and closes."""

    def __init__(self, factory: "FakeClientFactory", url: str):
        self.factory = factory
        self.url = url
        self.connects = 0
        self.closes = 0
        self.inputs: list[dict] = []

    def connect_sync(self):
        self.connects += 1
        return FakeSession(self)

    def close_sync(self):
        self.closes += 1
        if self.factory.raise_on_close:
            raise RuntimeError("the connection was already gone")

    def execute(self, document, variable_values=None):
        """The one-shot path: gql opens and closes a session around each call."""
        return self.record(variable_values, kind="one-shot")

    def record(self, variable_values, kind: str):
        payload = (variable_values or {}).get("input", {})
        with self.factory.lock:
            self.inputs.append(payload)
            self.factory.calls.append((self, kind, payload))
            if self.factory.fail_next:
                self.factory.fail_next = False
                raise RuntimeError("the transport died mid-answer")
        return {"addChatToken": payload}


class FakeClientFactory:
    """Replaces `new_appsync_client`, keeping every client it was asked for.

    Locked because the function publishes its thinking steps from background
    threads, which share this factory with the token stream under test.
    """

    def __init__(self):
        self.clients: list[FakeClient] = []
        self.calls: list[tuple] = []
        self.lock = threading.Lock()
        self.fail_next = False
        self.raise_on_close = False

    def __call__(self, url: str) -> FakeClient:
        client = FakeClient(self, url)
        with self.lock:
            self.clients.append(client)
        return client

    @property
    def token_calls(self) -> list[tuple]:
        """Published chat tokens only, leaving out the thinking steps."""
        return [call for call in self.calls if "ThinkingStep" not in call[2]]

    @property
    def token_clients(self) -> list[FakeClient]:
        seen: list[FakeClient] = []
        for client, _kind, _payload in self.token_calls:
            if client not in seen:
                seen.append(client)
        return seen


@pytest.fixture(scope="module")
def strands_stub():
    """The stand-in registered in sys.modules, whichever test file created it.

    Returning the registered object rather than a fresh one matters: the module
    under test resolves `from strands import Agent` through sys.modules, so a
    second stub object would be patched while the first stayed in use.
    """
    stub = sys.modules.setdefault("strands", types.ModuleType("strands"))
    stub.tool = lambda func: func
    stub.models = sys.modules.setdefault("strands.models", types.ModuleType("strands.models"))
    return stub


class UnusedAppsyncClient:
    """What the stand-in `asst_gql_client` offers in place of the real client.

    Constructing one means something reached past the `new_appsync_client` seam
    these tests replace, which would be a test that is not testing what it says,
    so it raises rather than pretending. The tool factories only import the name;
    the tool bodies that would construct it are not called here.
    """

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "the real AppSync client was constructed; these tests publish through the "
            "new_appsync_client seam and must not reach the transport layer"
        )


@pytest.fixture(scope="module")
def appsync_client_stub():
    """A stand-in for `asst_gql_client`, registered before the Lambda is loaded.

    The Lambda's tool factories import that module while building a tool, which
    is before any seam of ours is reachable, and importing the real one pulls in
    `gql.transport.requests` and so `requests_toolbelt` -- which this environment
    does not have and this suite does not need. Registered the way
    `lma-virtual-participant-stack/test/test_image_version_pruner.py` registers
    the sibling its Lambda imports, and the previous entry is restored afterwards
    so no other suite inherits it.
    """
    previous = sys.modules.get("asst_gql_client")
    stub = types.ModuleType("asst_gql_client")
    stub.AppsyncRequestsGqlClient = UnusedAppsyncClient
    sys.modules["asst_gql_client"] = stub
    try:
        yield stub
    finally:
        if previous is None:
            sys.modules.pop("asst_gql_client", None)
        else:
            sys.modules["asst_gql_client"] = previous


@pytest.fixture(scope="module")
def strands_module(strands_stub, appsync_client_stub):
    """The meeting-assist Lambda's module, imported by path under its own name.

    Its directory stays on sys.path for the rest of the session, appended last so
    it cannot shadow an installed package: the module imports its siblings lazily,
    inside the tool factories, which is also how Lambda runs it (the handler's own
    directory is on the path there). `bedrock_compat` is the one it imports at
    module scope, and it ships in that directory.
    """
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_REGION", "us-east-1")

    if str(FUNCTION_SRC) not in sys.path:
        sys.path.append(str(FUNCTION_SRC))
    spec = importlib.util.spec_from_file_location(
        "lma_strands_meeting_assist_streaming",
        FUNCTION_SRC / "strands_meeting_assist_function.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clients(strands_module, monkeypatch) -> FakeClientFactory:
    factory = FakeClientFactory()
    monkeypatch.setattr(strands_module, "new_appsync_client", factory)
    monkeypatch.setattr(strands_module, "APPSYNC_GRAPHQL_URL", APPSYNC_URL)
    return factory


# ── who has a subscriber ────────────────────────────────────────────────────


def test_the_chat_path_supplies_the_message_id_it_subscribed_with(strands_module):
    assert strands_module.subscriber_message_id(CHAT_SEGMENT_ARGS) == "chat-0b7f"


def test_the_wake_phrase_path_falls_back_to_the_segment_id(strands_module):
    """It writes that segment itself, so the id is one a client can know."""
    assert strands_module.subscriber_message_id(WAKE_PHRASE_SEGMENT_ARGS) == "6d2a"


def test_an_invocation_carrying_no_ids_has_no_subscriber(strands_module):
    """The Virtual Participant's voice path, which reads the returned payload."""
    assert strands_module.subscriber_message_id(VOICE_ASSISTANT_SEGMENT_ARGS) is None


# ── one connection per answer ───────────────────────────────────────────────


def test_every_token_of_one_answer_goes_out_on_one_connection(strands_module, clients):
    publisher = strands_module.AppsyncPublisher(APPSYNC_URL)
    for sequence in range(5):
        strands_module.send_chat_token_to_appsync(
            call_id="call-1",
            message_id="chat-0b7f",
            token=f"token {sequence}",
            is_complete=False,
            sequence=sequence,
            publisher=publisher,
        )

    assert len(clients.token_calls) == 5
    assert len(clients.token_clients) == 1, "a client was built more than once"
    assert clients.token_clients[0].connects == 1, "the session was reopened per token"


def test_the_publisher_releases_its_connection_when_the_answer_ends(strands_module, clients):
    publisher = strands_module.AppsyncPublisher(APPSYNC_URL)
    strands_module.send_chat_token_to_appsync(
        "call-1", "chat-0b7f", "a token", False, 0, publisher=publisher
    )
    publisher.close()
    assert clients.token_clients[0].closes == 1


def test_closing_twice_closes_the_connection_once(strands_module, clients):
    """`close()` is called from a finally and again by whatever ends the stream."""
    publisher = strands_module.AppsyncPublisher(APPSYNC_URL)
    strands_module.send_chat_token_to_appsync(
        "call-1", "chat-0b7f", "a token", False, 0, publisher=publisher
    )
    publisher.close()
    publisher.close()
    assert clients.token_clients[0].closes == 1


def test_a_connection_that_cannot_be_closed_does_not_end_the_answer(strands_module, clients):
    """The connection is being abandoned either way; raising here would mask the cause."""
    clients.raise_on_close = True
    publisher = strands_module.AppsyncPublisher(APPSYNC_URL)
    strands_module.send_chat_token_to_appsync(
        "call-1", "chat-0b7f", "a token", False, 0, publisher=publisher
    )
    publisher.close()


def test_a_failed_token_leaves_the_rest_of_the_answer_publishable(strands_module, clients):
    """The per-token error handling must not poison the connection it is holding.

    A session that has just raised is not reused: it is discarded, and the next
    token opens a fresh one. So a single transport failure costs one reconnect
    rather than every token after it.
    """
    publisher = strands_module.AppsyncPublisher(APPSYNC_URL)
    strands_module.send_chat_token_to_appsync(
        "call-1", "chat-0b7f", "first", False, 0, publisher=publisher
    )
    clients.fail_next = True
    # The failure is swallowed and logged, as it was before: one lost token must
    # not abandon the answer.
    strands_module.send_chat_token_to_appsync(
        "call-1", "chat-0b7f", "second", False, 1, publisher=publisher
    )
    strands_module.send_chat_token_to_appsync(
        "call-1", "chat-0b7f", "third", False, 2, publisher=publisher
    )

    assert len(clients.token_clients) == 2, "the failed session was reused"
    assert clients.token_clients[0].closes == 1, "the failed session was not released"
    assert [call[2]["Token"] for call in clients.token_calls] == ["first", "second", "third"]


def test_a_caller_without_a_publisher_still_sends_its_token(strands_module, clients):
    """Keeps the one-shot path working for any caller that has no answer to stream."""
    strands_module.send_chat_token_to_appsync("call-1", "chat-0b7f", "lone", True, 0)
    assert [call[1] for call in clients.token_calls] == ["one-shot"]


def test_the_mutation_document_is_parsed_once_and_names_addchattoken(strands_module):
    """The document is a constant, so it is parsed once per execution environment.

    gql re-parses the string on every `gql()` call, and the mutation does not
    vary by token, so publishing an answer need pay for the parse only once.

    Asserted on the parsed AST rather than on the text, so a document that does
    not send `addChatToken` fails here even if the string survives in a comment
    somewhere.
    """
    first = strands_module.get_chat_token_mutation()
    assert strands_module.get_chat_token_mutation() is first
    selections = first.definitions[0].selection_set.selections
    assert [selection.name.value for selection in selections] == ["addChatToken"]


# ── the streaming branch, end to end ────────────────────────────────────────


def _stub_agent(strands_stub, monkeypatch, chunks: tuple[str, ...]) -> list[str]:
    """A Strands agent that streams `chunks` and answers with them joined.

    Returns the list it records its entry point into: "stream" when the handler
    streamed the answer, "call" when it took the non-streaming path. That is how
    the skip is asserted -- a branch that was taken -- rather than by the absence
    of published tokens, which anything failing earlier would also produce.
    """
    entry_points: list[str] = []

    class StubAgent:
        def __init__(self, model=None, system_prompt=None, tools=None, hooks=None):
            pass

        async def stream_async(self, _message):
            entry_points.append("stream")
            for chunk in chunks:
                yield {"data": chunk}

        def __call__(self, _message):
            entry_points.append("call")
            return "".join(chunks)

    class StubBedrockModel:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(strands_stub, "Agent", StubAgent, raising=False)
    monkeypatch.setattr(strands_stub.models, "BedrockModel", StubBedrockModel, raising=False)
    return entry_points


def _invoke(strands_module, strands_stub, monkeypatch, segment_args: dict, chunks):
    """Drive the real handler, and refuse to let it fail quietly.

    The handler wraps everything in a broad `except`, so an exception on the way
    -- a missing import in a thin environment, say -- becomes a `statusCode` 500
    payload rather than a raise. A test asserting that nothing was published
    would then pass for entirely the wrong reason, so the handler's completion is
    a precondition here, reported with whatever it did return. Streaming being
    configured is asserted for the same reason: without it, "no tokens were
    published" says nothing about the subscriber rule.
    """
    monkeypatch.setattr(strands_module, "MCP_LOADER_AVAILABLE", False)
    monkeypatch.setattr(strands_module, "ENABLE_STREAMING", True)
    monkeypatch.setattr(strands_module, "EVENT_API_HTTP_URL", "")
    monkeypatch.setenv("KB_ID", "")
    monkeypatch.setenv("TRANSCRIPT_KB_ID", "")
    entry_points = _stub_agent(strands_stub, monkeypatch, chunks)

    assert strands_module.ENABLE_STREAMING, "streaming must be on for these tests to mean anything"
    assert strands_module.APPSYNC_GRAPHQL_URL, "an AppSync endpoint must be configured"

    event = {
        "userInput": "summarise this meeting",
        "callId": "call-1",
        "userEmail": "someone@example.com",
    }
    if segment_args:
        event["transcript_segment_args"] = segment_args
    result = strands_module.handler(event, None)

    assert "message" in result, (
        f"the handler did not answer; it returned {result}. Nothing below this point "
        "describes the behaviour under test."
    )
    assert entry_points, "the agent was never reached"
    return result, entry_points


def test_an_answer_with_a_subscriber_is_streamed_over_a_single_connection(
    strands_module, strands_stub, clients, monkeypatch
):
    """Three publishes -- two tokens and the completion marker -- one connection.

    This is also what demonstrates that publishing works at all in this
    environment, which is what makes the empty list in the test below evidence of
    the skip rather than of something broken.
    """
    result, entry_points = _invoke(
        strands_module, strands_stub, monkeypatch, CHAT_SEGMENT_ARGS, ("Hello ", "world")
    )

    assert result["message"] == "Hello world"
    assert entry_points == ["stream"]
    tokens = [call[2] for call in clients.token_calls]
    assert [token["Token"] for token in tokens] == ["Hello ", "world", ""]
    assert tokens[-1]["IsComplete"] is True
    assert len(clients.token_clients) == 1
    assert clients.token_clients[0].connects == 1
    assert clients.token_clients[0].closes == 1, "the connection outlived the answer"


def test_an_answer_with_no_subscriber_publishes_no_tokens(
    strands_module, strands_stub, clients, monkeypatch
):
    """The Virtual Participant's voice path: the answer comes back, nothing is published.

    Every publish on that path reached a mutation that broadcasts to nobody and
    stores nothing, so it was time added to a spoken answer for no result.

    The non-streaming entry point having been used is the positive half of this
    claim, and is asserted first: the handler chose that branch and answered from
    it. The empty token list on its own would also be produced by a handler that
    never got as far as answering.
    """
    result, entry_points = _invoke(
        strands_module,
        strands_stub,
        monkeypatch,
        VOICE_ASSISTANT_SEGMENT_ARGS,
        ("Hello ", "world"),
    )

    assert entry_points == ["call"], "the streaming branch was taken after all"
    assert result["message"] == "Hello world"
    assert clients.token_calls == []


def test_the_wake_phrase_path_is_still_streamed(
    strands_module,
    strands_stub,
    clients,
    monkeypatch,
):
    """It supplies a SegmentId, so a subscriber could be listening; keep publishing.

    The skip has to be decided by what the payload carries, not by guessing which
    caller sent it, and it has to fail in this direction -- which is why this test
    asserts the branch taken as well as the tokens published, and why `_invoke`
    refuses an invocation that did not answer.
    """
    _result, entry_points = _invoke(
        strands_module, strands_stub, monkeypatch, WAKE_PHRASE_SEGMENT_ARGS, ("Hello ", "world")
    )
    assert entry_points == ["stream"]
    assert [call[2]["MessageId"] for call in clients.token_calls] == ["6d2a"] * 3
