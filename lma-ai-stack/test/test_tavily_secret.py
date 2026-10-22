# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for tavily_secret, which supplies the meeting assistant's web search key.

The web search tool's API key lives in AWS Secrets Manager and the meeting
assist Lambda is given only the ARN of the secret. Three properties of that
arrangement are worth asserting, because each is invisible in normal operation
and expensive in a different way when it breaks:

* A deployment with no Tavily API key must never call Secrets Manager. The
  decision to offer the tool at all is made from the ARN, and web search is off
  by default, so a regression here would add a per-invocation API call to every
  deployment that does not use the feature.
* A deployment that does use it must call Secrets Manager at most once per
  execution environment, not once per search.
* A failed fetch must not be cached, or one throttled call would leave web
  search unavailable for the rest of that container's life.

The module is loaded from the meeting-assist setup stack's deployment package
by path, the same way test_bedrock_compat.py loads its subject.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "lma-meetingassist-setup-stack" / "src" / "tavily_secret.py"
HANDLER_PATH = (
    REPO / "lma-meetingassist-setup-stack" / "src" / "strands_meeting_assist_function.py"
)

ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:lma/integrations/tavily-api-key"


class _FakeSecretsManager:
    """Stands in for the Secrets Manager client, counting its calls."""

    def __init__(self, *, value: str | None = "tavily-key", error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls: list[str] = []

    def get_secret_value(self, SecretId: str):  # noqa: N803 - boto3's parameter name
        self.calls.append(SecretId)
        if self.error is not None:
            raise self.error
        return {} if self.value is None else {"SecretString": self.value}


class _ExplodingSecretsManager:
    """Fails the test if Secrets Manager is reached at all."""

    def get_secret_value(self, SecretId: str):  # noqa: N803 - boto3's parameter name
        raise AssertionError(f"Secrets Manager must not be called, but was for {SecretId}")


def _load(monkeypatch: pytest.MonkeyPatch, secret_arn: str):
    """Import a fresh copy of the module with TAVILY_API_KEY_SECRET_ARN set.

    A fresh copy per test because the module reads the environment once, at
    import, and caches the fetched key in a module-level variable — which is
    exactly the behaviour under test.
    """
    monkeypatch.setenv("TAVILY_API_KEY_SECRET_ARN", secret_arn)
    spec = importlib.util.spec_from_file_location("tavily_secret_under_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_web_search_is_not_configured_without_a_secret_arn(monkeypatch: pytest.MonkeyPatch):
    module = _load(monkeypatch, "")
    assert module.is_configured() is False


def test_no_secret_is_read_when_no_secret_arn_is_configured(monkeypatch: pytest.MonkeyPatch):
    module = _load(monkeypatch, "")
    module._CLIENT = _ExplodingSecretsManager()  # pylint: disable=protected-access
    assert module.get_api_key() == ""
    # Repeated so a caching mistake cannot make the first call the only one
    # that stays cheap.
    assert module.get_api_key() == ""


def test_web_search_is_configured_from_the_secret_arn_alone(monkeypatch: pytest.MonkeyPatch):
    module = _load(monkeypatch, ARN)
    module._CLIENT = _ExplodingSecretsManager()  # pylint: disable=protected-access
    assert module.is_configured() is True


def test_the_api_key_is_fetched_from_the_arn_it_was_given(monkeypatch: pytest.MonkeyPatch):
    module = _load(monkeypatch, ARN)
    fake = _FakeSecretsManager(value="tavily-key")
    module._CLIENT = fake  # pylint: disable=protected-access
    assert module.get_api_key() == "tavily-key"
    assert fake.calls == [ARN]


def test_the_api_key_is_fetched_once_per_execution_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load(monkeypatch, ARN)
    fake = _FakeSecretsManager(value="tavily-key")
    module._CLIENT = fake  # pylint: disable=protected-access
    for _ in range(5):
        assert module.get_api_key() == "tavily-key"
    assert len(fake.calls) == 1


def test_surrounding_whitespace_is_stripped_from_the_stored_value(
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load(monkeypatch, ARN)
    module._CLIENT = _FakeSecretsManager(value="  tavily-key\n")  # pylint: disable=protected-access
    assert module.get_api_key() == "tavily-key"


def test_a_failed_fetch_is_reported_as_unavailable_and_retried(
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load(monkeypatch, ARN)
    failing = _FakeSecretsManager(error=RuntimeError("throttled"))
    module._CLIENT = failing  # pylint: disable=protected-access
    assert module.get_api_key() == ""
    assert len(failing.calls) == 1

    # The next search tries again rather than inheriting the empty result.
    working = _FakeSecretsManager(value="tavily-key")
    module._CLIENT = working  # pylint: disable=protected-access
    assert module.get_api_key() == "tavily-key"
    assert working.calls == [ARN]


def test_a_secret_with_no_value_is_reported_as_unavailable(monkeypatch: pytest.MonkeyPatch):
    module = _load(monkeypatch, ARN)
    module._CLIENT = _FakeSecretsManager(value=None)  # pylint: disable=protected-access
    assert module.get_api_key() == ""


def test_reset_cache_drops_the_cached_key(monkeypatch: pytest.MonkeyPatch):
    module = _load(monkeypatch, ARN)
    fake = _FakeSecretsManager(value="tavily-key")
    module._CLIENT = fake  # pylint: disable=protected-access
    assert module.get_api_key() == "tavily-key"
    module.reset_cache()
    module._CLIENT = fake  # pylint: disable=protected-access
    assert module.get_api_key() == "tavily-key"
    assert len(fake.calls) == 2


def test_the_handler_decides_on_web_search_without_reading_a_key_from_the_environment():
    """The call site, asserted as text rather than by importing the handler.

    `strands_meeting_assist_function.py` pulls in the Strands SDK and the
    Bedrock layer, neither of which is installed for these tests. What matters
    here is small enough to read from the source: the tool is registered from
    `tavily_secret.is_configured()`, and no plaintext key is read from the
    function's environment.
    """
    source = HANDLER_PATH.read_text()
    assert "tavily_secret.is_configured()" in source
    assert "tavily_secret.get_api_key()" in source
    assert "TAVILY_API_KEY" not in source
