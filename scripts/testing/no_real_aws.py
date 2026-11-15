# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Keep unit tests from reaching AWS.

Each unit-test root's ``conftest.py`` star-imports this module and calls
:func:`activate`, so every test under it is covered without opting in. The roots
are those CI runs: ``lma-ai-stack/`` (its Lambda functions, its layers and its
``test/``), ``lma-meetingassist-setup-stack/test``, ``lma-virtual-participant-stack/test``,
the ASR stack's ``lambda_functions/``, the SDK's and the CLI's ``tests/``, and
``integ-tests/test_ci_plumbing.py``. (The ASR runtime's own suite, ``make
test-asr``, runs in a virtualenv without botocore, so it has nothing to guard.)
Two things happen:

* **Fake credentials.** On :func:`activate`, the AWS credential and profile variables are
  replaced with fake ones and the instance metadata lookup is turned off, so a
  client built by a test, or at import by the module under test, signs with
  credentials no AWS account accepts. Skipped for a session run with
  ``LMA_EVALUATE_APPSYNC=1``, whose opted-in tests need the caller's own
  credentials.
* **No request leaves the process.** ``botocore``'s ``Endpoint._send``, the one
  place a request goes onto the network, is replaced. A test that sends one fails
  -- even when the code under test catches the exception, as the meeting
  assistant's direct-Bedrock fallback does, because the attempt is recorded and
  the test is failed when it ends. A request made outside a test, at import or
  collection, raises there.

botocore's ``Stubber`` and moto's ``mock_aws`` answer a request before it reaches
``_send``, so tests that use them need nothing. A test that has to call AWS for
real (the AppSync template evaluations) carries ``@pytest.mark.real_aws``.

HTTP sent other than through botocore (``requests``, ``urllib``) is not covered.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

#: The marker a test carries to be allowed to send requests to AWS.
MARKER = "real_aws"

#: A session run with this set to "1" keeps the caller's credentials.
REAL_CREDENTIALS_ENV_VAR = "LMA_EVALUATE_APPSYNC"

FAKE_CREDENTIALS = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "unit-tests-must-not-call-aws",
    "AWS_SESSION_TOKEN": "unit-tests-must-not-call-aws",
    "AWS_EC2_METADATA_DISABLED": "true",
}
#: Removed so neither a profile nor a shared credentials file supplies real ones.
CREDENTIAL_SOURCES = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
)

#: ``allowed`` is whether a request may be sent right now: True until
#: :func:`activate` makes the whole process strict, and set per test by
#: :func:`_no_real_aws`.
_STATE: dict[str, Any] = {"allowed": True, "blocked": []}


class RealAwsCallBlocked(RuntimeError):
    """A unit test, or the code it ran, tried to send a request to AWS."""


def _describe(request: Any) -> str:
    target = request.headers.get("X-Amz-Target") if getattr(request, "headers", None) else None
    if isinstance(target, bytes):
        target = target.decode("utf-8", "replace")
    url = getattr(request, "url", "?")
    return f"{getattr(request, 'method', '?')} {url}" + (f" ({target})" if target else "")


def install_guard() -> None:
    """Replace botocore's network send with one that refuses (once per process)."""
    try:
        from botocore import endpoint  # pylint: disable=import-outside-toplevel
    except ImportError:  # a suite that runs without botocore has nothing to guard
        return
    if getattr(endpoint.Endpoint._send, "_lma_no_real_aws", False):
        return
    original = endpoint.Endpoint._send

    def _send(self: Any, request: Any) -> Any:
        if _STATE["allowed"]:
            return original(self, request)
        described = _describe(request)
        _STATE["blocked"].append(described)
        raise RealAwsCallBlocked(
            f"a unit test sent a request to AWS: {described}. Stub the client, or mark "
            f"the test @pytest.mark.{MARKER} if it must call AWS"
        )

    _send._lma_no_real_aws = True  # type: ignore[attr-defined]
    endpoint.Endpoint._send = _send


def use_fake_credentials() -> None:
    """Point the process at credentials no AWS account accepts."""
    if os.environ.get(REAL_CREDENTIALS_ENV_VAR) == "1":
        return
    for name in CREDENTIAL_SOURCES:
        os.environ.pop(name, None)
    os.environ.update(FAKE_CREDENTIALS)
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    os.environ["AWS_CONFIG_FILE"] = os.devnull
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_REGION", os.environ["AWS_DEFAULT_REGION"])


def activate() -> None:
    """Refuse every request this process sends outside a marked test; fake the credentials.

    Call it at the top of a unit-test root's conftest.py, before anything builds a
    client: test modules are imported after their conftest, so a client a module
    builds at import gets the fake credentials, and a request sent at import or
    collection is refused.

    A test module that shares its session with tests that do call AWS (the
    integration tests) calls :func:`install_guard` instead and imports
    :func:`_no_real_aws` and :func:`fake_aws_credentials`, which guard only its
    own tests.
    """
    install_guard()
    _STATE["allowed"] = False
    use_fake_credentials()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", f"{MARKER}: the test sends requests to AWS and is allowed to"
    )


@pytest.fixture(autouse=True)
def _no_real_aws(request: pytest.FixtureRequest):
    """Fail a test that sent a request to AWS without carrying the marker."""
    blocked: list[str] = _STATE["blocked"]
    start = len(blocked)
    outside = _STATE["allowed"]
    _STATE["allowed"] = request.node.get_closest_marker(MARKER) is not None
    try:
        yield
    finally:
        _STATE["allowed"] = outside
    sent = blocked[start:]
    if sent:
        pytest.fail(
            "this test sent requests to AWS (unit tests must not; mark it "
            f"@pytest.mark.{MARKER} if it has to): " + "; ".join(sent),
            pytrace=False,
        )


@pytest.fixture
def fake_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """:func:`use_fake_credentials` for one test, for a module that cannot use it for all."""
    for name in CREDENTIAL_SOURCES:
        monkeypatch.delenv(name, raising=False)
    for name, value in FAKE_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)


#: What a conftest.py star-imports: the hook and the autouse fixture.
__all__ = ["_no_real_aws", "pytest_configure"]
