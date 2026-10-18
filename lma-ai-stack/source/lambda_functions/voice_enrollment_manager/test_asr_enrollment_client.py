# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for turning enrollment recordings into embeddings.

Three promises are being kept here.

The audio is never written anywhere, which is what the terms the enrollee
agreed to say. These tests cannot prove a negative about the filesystem, but
they do pin the two things that would make a leak possible: the recordings are
decoded in memory and handed straight to the session, and nothing in this
module takes a path or a bucket.

A MicroVM acquired to embed is always released. One left running costs money
until its own max-duration backstop expires, and the release has to happen on
the failure paths too -- which is where it would be easy to leave out.

And a request that is not a plausible enrollment is refused BEFORE anything is
acquired, so a malformed call cannot start an engine.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load():
    spec = importlib.util.spec_from_file_location(
        "asr_enrollment_client", HERE / "asr_enrollment_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


client = _load()

LAUNCHER = "arn:aws:lambda:us-east-1:123456789012:function:asr-launcher"
# 2 bytes per sample, 16 kHz: three seconds of 16-bit mono.
CLIP = base64.b64encode(b"\x01\x00" * 48000).decode()


class FakePayload:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body


class FakeLambda:
    """Records every launcher call, so acquire/release pairing is checkable."""

    def __init__(self, *, acquire=None, acquire_error=False):
        self.calls = []
        self.acquire = acquire if acquire is not None else {
            "ok": True,
            "microvmId": "mvm-1",
            "endpoint": "example.invalid/session",
            "authToken": "token-abc",
        }
        self.acquire_error = acquire_error

    def invoke(self, **kwargs):
        payload = json.loads(kwargs["Payload"].decode("utf-8"))
        self.calls.append(payload)
        if payload["action"] == "acquire":
            if self.acquire_error:
                raise RuntimeError("launcher unreachable")
            return {"Payload": FakePayload(self.acquire)}
        return {"Payload": FakePayload({"ok": True})}

    def actions(self):
        return [call["action"] for call in self.calls]


def install(monkeypatch, *, vectors=None, embed_error=None, **kwargs):
    fake = FakeLambda(**kwargs)
    monkeypatch.setattr(client, "lambda_client", fake)
    monkeypatch.setattr(client, "ASR_LAUNCHER_FUNCTION_ARN", LAUNCHER)
    monkeypatch.setattr(client, "ASR_DIRECT_ENDPOINT", "")

    seen = {}

    async def fake_embed(frames, endpoint, auth_token):
        seen["frames"] = frames
        seen["endpoint"] = endpoint
        seen["auth_token"] = auth_token
        if embed_error:
            raise embed_error
        return vectors if vectors is not None else [[1.0, 0.0]] * len(frames)

    monkeypatch.setattr(client, "_embed", fake_embed)
    return fake, seen


def test_a_voiceprint_session_is_acquired_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _ = install(monkeypatch)
    assert client.embed_utterances([CLIP, CLIP, CLIP])
    assert fake.actions() == ["acquire", "release"]
    assert fake.calls[1]["microvmId"] == "mvm-1"


def test_the_microvm_is_released_even_when_embedding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path where a leak would cost money until the backstop expired."""
    fake, _ = install(monkeypatch, embed_error=RuntimeError("engine died"))
    with pytest.raises(RuntimeError):
        client.embed_utterances([CLIP, CLIP, CLIP])
    assert fake.actions() == ["acquire", "release"]


def test_the_microvm_is_released_when_no_vectors_come_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _ = install(monkeypatch, vectors=[])
    with pytest.raises(RuntimeError):
        client.embed_utterances([CLIP, CLIP, CLIP])
    assert fake.actions() == ["acquire", "release"]


def test_a_malformed_request_acquires_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused before an engine is started, not after."""
    fake, _ = install(monkeypatch)
    with pytest.raises(ValueError):
        client.embed_utterances(["not base64 at all!!"])
    assert fake.actions() == []


def test_too_many_recordings_acquires_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = install(monkeypatch)
    with pytest.raises(ValueError):
        client.embed_utterances([CLIP] * (client.MAX_UTTERANCES + 1))
    assert fake.actions() == []


def test_no_recordings_acquires_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = install(monkeypatch)
    with pytest.raises(ValueError):
        client.embed_utterances([])
    assert fake.actions() == []


def test_the_session_is_reached_over_tls_with_the_minted_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy reads the token from the subprotocol; nothing else gets through."""
    _, seen = install(monkeypatch)
    client.embed_utterances([CLIP, CLIP, CLIP])
    assert seen["endpoint"] == "wss://example.invalid/session"
    assert seen["auth_token"] == "token-abc"
    assert client._subprotocols("token-abc") == [
        "lambda-microvms",
        "lambda-microvms.authentication.token-abc",
        f"lambda-microvms.port.{client.ASR_PORT}",
    ]


def test_a_direct_endpoint_needs_no_lease_and_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The development hatch talks to an already-running server."""
    fake, seen = install(monkeypatch)
    monkeypatch.setattr(client, "ASR_DIRECT_ENDPOINT", "ws://localhost:8080")
    client.embed_utterances([CLIP, CLIP, CLIP])
    assert fake.actions() == []
    assert seen["endpoint"] == "ws://localhost:8080"
    assert seen["auth_token"] == ""
    assert client._subprotocols("") == []


def test_no_launcher_configured_is_refused_rather_than_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    monkeypatch.setattr(client, "ASR_LAUNCHER_FUNCTION_ARN", "")
    with pytest.raises(RuntimeError, match="no ASR engine"):
        client.embed_utterances([CLIP, CLIP, CLIP])


def test_a_launcher_that_refuses_yields_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = install(monkeypatch, acquire={"ok": False, "reason": "at capacity"})
    with pytest.raises(RuntimeError, match="no ASR engine"):
        client.embed_utterances([CLIP, CLIP, CLIP])
    # Nothing was acquired, so there is nothing to release.
    assert fake.actions() == ["acquire"]


def test_a_launcher_that_errors_yields_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = install(monkeypatch, acquire_error=True)
    with pytest.raises(RuntimeError, match="no ASR engine"):
        client.embed_utterances([CLIP, CLIP, CLIP])
    assert fake.actions() == ["acquire"]


def test_each_enrolment_uses_its_own_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two enrolments must not collide on one call id in the launcher."""
    fake, _ = install(monkeypatch)
    client.embed_utterances([CLIP, CLIP, CLIP])
    client.embed_utterances([CLIP, CLIP, CLIP])
    ids = [call["callId"] for call in fake.calls if call["action"] == "acquire"]
    assert len(set(ids)) == 2


def test_empty_vectors_are_dropped_but_a_usable_one_is_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clip the embedder could not use must not drag the whole enrolment down."""
    install(monkeypatch, vectors=[[], [0.5, 0.5], []])
    assert client.embed_utterances([CLIP, CLIP, CLIP]) == [[0.5, 0.5]]


def test_this_module_writes_the_audio_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """The terms promise the recordings are discarded, so there is nothing to write with.

    A source-level check rather than a behavioural one: it catches somebody
    adding a debug dump, which is exactly how audio like this escapes.
    """
    source = (HERE / "asr_enrollment_client.py").read_text(encoding="utf-8")
    for forbidden in ("open(", "put_object", "s3", "write_text", "write_bytes", "tempfile"):
        assert forbidden not in source, f"{forbidden!r} must not appear here"


def test_the_embed_budget_fits_inside_appsyncs_request_ceiling() -> None:
    """The only caller is an AppSync resolver, whose ceiling is 30s and fixed.

    A longer budget is not a longer wait -- it is a wait nobody is listening
    to, after which this function can still store a voiceprint the enrollee was
    told had failed.
    """
    assert client.EMBED_TIMEOUT_SECONDS <= 25


def test_the_total_payload_bound_is_smaller_than_the_product_of_the_per_clip_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-clip bounds multiply, so a total bound is what actually binds.

    The per-clip limit times the count used to come to far more than the
    request carrying them could possibly have been, so the guard did not guard.
    """
    assert client.MAX_TOTAL_BYTES < client.MAX_UTTERANCES * client.MAX_UTTERANCE_BYTES


def test_too_much_audio_in_total_acquires_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = install(monkeypatch)
    big = base64.b64encode(b"\x01\x00" * (client.MAX_UTTERANCE_BYTES // 2)).decode()
    count = client.MAX_TOTAL_BYTES // client.MAX_UTTERANCE_BYTES + 2
    with pytest.raises(ValueError, match="in total"):
        client.embed_utterances([big] * count)
    assert fake.actions() == []


def test_a_realistic_enrolment_is_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bounds must not refuse the enrolment the portal actually asks for.

    Six clips of about five seconds is what reading the script produces, and it
    measured 1.16 MB base64 against a live stack.
    """
    install(monkeypatch)
    five_seconds = base64.b64encode(b"\x01\x00" * (5 * 16000)).decode()
    assert client.embed_utterances([five_seconds] * 6)
