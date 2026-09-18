# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Round-trip tests for the /vnc/* access token.

The token is minted here (index.mint_token) and checked by the Lambda@Edge
function, whose source lives as a string literal in edge_auth_deployer because
Lambda@Edge cannot attach layers. The two halves therefore cannot share a module,
so these tests exercise the *real* edge source: they substitute its deploy-time
placeholders, exec it, and drive its handler. A format change on one side that is
not mirrored on the other fails here rather than at the edge.

No AWS calls: DynamoDB is stubbed, the minting secret is injected, and the edge
module's secret cache is pre-seeded.
"""

from __future__ import annotations

import base64
import importlib.util
import itertools
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:k"

# index.py builds boto3 clients at import time, and client construction resolves
# an endpoint even though every call below is stubbed. CI has no ambient AWS
# config, so without a region the IMPORT raises NoRegionError.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("VP_TABLE_NAME", "vp-table")
os.environ.setdefault("SIGNING_SECRET_ARN", SECRET_ARN)

HERE = Path(__file__).resolve().parent
DEPLOYER_DIR = HERE.parent / "edge_auth_deployer"
MICROVM_TOKEN_DIR = HERE.parent / "microvm_vnc_token"
if str(HERE) not in sys.path:
    # index.py imports its sibling vp_access; make test-lambdas runs pytest from
    # inside this directory, but a direct `pytest <path>` invocation may not.
    sys.path.insert(0, str(HERE))


def _load_by_path(name: str, path: Path):
    """Import a module from an explicit path.

    Every Lambda source directory in this tree has its own ``index.py``, so a
    plain ``import index`` would resolve to whichever directory happens to come
    first on sys.path. Both modules are needed here, so both are named.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


minter = _load_by_path("vnc_edge_token_index", HERE / "index.py")
deployer = _load_by_path("edge_auth_deployer_index", DEPLOYER_DIR / "index.py")

SECRET = "a-test-signing-secret"
VP_ID = "vp-abc123"
URI = f"/vnc/{VP_ID}"


_EDGE_SOURCE_DIR = tempfile.mkdtemp(prefix="vnc-edge-source-")
_edge_load_count = itertools.count()


def load_edge_module():
    """Import the real edge source with its placeholders filled in.

    Written to a scratch file and imported rather than kept as a string, so it is
    an ordinary module import and a traceback points at real lines. Each call
    gets a fresh module name so tests that mutate module state do not affect one
    another.

    The secret cache is pre-seeded so ``get_signing_secret`` never reaches
    Secrets Manager.
    """
    code = deployer.render_edge_code(SECRET_ARN, "us-east-1")
    assert "PLACEHOLDER" not in code, "deploy-time configuration was not substituted"
    name = f"vnc_edge_authorizer_{next(_edge_load_count)}"
    path = Path(_EDGE_SOURCE_DIR) / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    module = _load_by_path(name, path)
    module._signing_secret.append(SECRET)
    return module


@pytest.fixture(name="edge")
def edge_fixture():
    return load_edge_module()


def viewer_request(token, uri=URI):
    querystring = f"token={token}" if token is not None else ""
    return {"Records": [{"cf": {"request": {"uri": uri, "querystring": querystring}}}]}


def mint(vp_id=VP_ID, ttl_seconds=300, secret=SECRET):
    return minter.mint_token(secret, vp_id, int(time.time()) + ttl_seconds)


def is_forwarded(response):
    """True when the edge function returned the request rather than a denial."""
    return "status" not in response


# --------------------------------------------------------------------------
# Token format
# --------------------------------------------------------------------------


def test_token_is_payload_and_signature_joined_by_a_dot() -> None:
    token = mint()
    parts = token.split(".")
    assert len(parts) == 2
    payload = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    assert payload["vpId"] == VP_ID
    assert payload["prefix"] == URI
    assert payload["port"] == minter.NOVNC_PORT
    assert payload["exp"] > time.time()


def test_token_lifetime_is_measured_in_minutes() -> None:
    """A copied URL stops working quickly; the viewer re-mints on reconnect."""
    assert 0 < minter.TOKEN_TTL_SECONDS <= 900


# --------------------------------------------------------------------------
# Round trip through the real edge source
# --------------------------------------------------------------------------


def test_accepts_a_freshly_minted_token(edge) -> None:
    response = edge.lambda_handler(viewer_request(mint()), None)
    assert is_forwarded(response), response


def test_rejects_token_past_its_expiry(edge) -> None:
    response = edge.lambda_handler(viewer_request(mint(ttl_seconds=-1)), None)
    assert response["status"] == "403"


def test_rejects_token_issued_for_another_participant(edge) -> None:
    """The signed payload, not the path, decides which VP a token is good for."""
    other = mint(vp_id="vp-somebody-else")
    response = edge.lambda_handler(viewer_request(other, uri=URI), None)
    assert response["status"] == "403"


def test_rejects_token_with_invalid_signature(edge) -> None:
    payload_b64, signature_b64 = mint().split(".")
    flipped = ("A" if signature_b64[0] != "A" else "B") + signature_b64[1:]
    response = edge.lambda_handler(viewer_request(f"{payload_b64}.{flipped}"), None)
    assert response["status"] == "403"


def test_rejects_payload_edited_after_signing(edge) -> None:
    """Re-encoding the payload with a later expiry invalidates the MAC."""
    payload_b64, signature_b64 = mint(ttl_seconds=-1).split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    payload["exp"] = int(time.time()) + 3600
    edited = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    response = edge.lambda_handler(viewer_request(f"{edited}.{signature_b64}"), None)
    assert response["status"] == "403"


def test_rejects_token_signed_with_a_different_key(edge) -> None:
    response = edge.lambda_handler(viewer_request(mint(secret="not-the-key")), None)
    assert response["status"] == "403"


def test_missing_token_is_reported_as_unauthorized(edge) -> None:
    """401 distinguishes "no credential offered" from "credential refused"."""
    response = edge.lambda_handler(viewer_request(None), None)
    assert response["status"] == "401"


def test_rejects_a_malformed_token_without_raising(edge) -> None:
    for bad in ["", "no-dot", "a.b.c", "!!!.???", "."]:
        response = edge.lambda_handler(viewer_request(bad), None)
        assert response["status"] in ("401", "403"), bad


def test_denies_when_the_signing_secret_cannot_be_read() -> None:
    """Fail closed: an unreadable secret must not turn into an open path."""
    module = load_edge_module()
    module._signing_secret.clear()

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("no credentials")

    module.boto3 = types.SimpleNamespace(client=unavailable)
    response = module.lambda_handler(viewer_request(mint()), None)
    assert response["status"] == "403"


def test_paths_outside_the_vnc_behavior_pass_through(edge) -> None:
    request = {"Records": [{"cf": {"request": {"uri": "/index.html", "querystring": ""}}}]}
    assert is_forwarded(edge.lambda_handler(request, None))


# --------------------------------------------------------------------------
# Handler: authorization and response shape
# --------------------------------------------------------------------------


class _FakeDynamo:
    def __init__(self, item):
        self.item = item

    def get_item(self, TableName, Key):  # noqa: N803 - boto3 kwarg names
        return {"Item": self.item} if self.item else {}


def _install_stubs(monkeypatch, item):
    monkeypatch.setattr(minter, "dynamodb", _FakeDynamo(item))
    monkeypatch.setattr(minter, "get_signing_secret", lambda: SECRET)


def test_handler_mints_for_the_owner(monkeypatch: pytest.MonkeyPatch, edge) -> None:
    _install_stubs(
        monkeypatch,
        {"Owner": {"S": "bob@example.com"}, "vncPassword": {"S": "sEcReT12"}},
    )
    result = minter.lambda_handler(
        {
            "arguments": {"vpId": VP_ID},
            "identity": {"username": "bob@example.com", "claims": {}},
        },
        None,
    )
    assert result["expiresAt"].endswith("Z")
    assert result["vncPassword"] == "sEcReT12"
    # The minted token really is accepted by the edge function.
    assert is_forwarded(edge.lambda_handler(viewer_request(result["token"]), None))


def test_handler_refuses_a_caller_without_access(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stubs(monkeypatch, {"Owner": {"S": "alice@example.com"}})
    with pytest.raises(Exception, match="Not authorized"):
        minter.lambda_handler(
            {
                "arguments": {"vpId": VP_ID},
                "identity": {"username": "bob@example.com", "claims": {}},
            },
            None,
        )


def test_handler_accepts_a_caller_the_vp_was_shared_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(
        monkeypatch,
        {"Owner": {"S": "alice@example.com"}, "SharedWith": {"S": "bob@example.com, carol@x"}},
    )
    result = minter.lambda_handler(
        {"arguments": {"vpId": VP_ID}, "identity": {"username": "bob@example.com"}},
        None,
    )
    assert result["token"]
    # No credential recorded yet: the field is null rather than an empty string,
    # so the viewer can tell the two apart.
    assert result["vncPassword"] is None


def test_handler_requires_an_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stubs(monkeypatch, {"Owner": {"S": "bob@example.com"}})
    with pytest.raises(Exception, match="Unauthenticated"):
        minter.lambda_handler({"arguments": {"vpId": VP_ID}, "identity": {}}, None)


def test_handler_requires_a_vp_id() -> None:
    with pytest.raises(Exception, match="vpId is required"):
        minter.lambda_handler({"arguments": {}, "identity": {"username": "bob"}}, None)


# --------------------------------------------------------------------------
# The shared authorization module
# --------------------------------------------------------------------------


def test_vp_access_copies_are_identical() -> None:
    """Both minting resolvers must decide access the same way.

    Lambda packages are built per CodeUri and cannot import across directories,
    so vp_access.py is copied into each function's source directory (the
    convention microvm_client.py already follows). This pins the copies together.
    """
    mine = (HERE / "vp_access.py").read_bytes()
    theirs = (MICROVM_TOKEN_DIR / "vp_access.py").read_bytes()
    assert mine == theirs, (
        "vp_access.py differs between vnc_edge_token/ and microvm_vnc_token/; "
        "apply the change to both copies"
    )


def test_caller_identity_precedence_includes_identity_username() -> None:
    """identity.username must stay in the chain.

    A live call failed because only the Cognito claims were consulted and that
    deployment populates identity.username instead.
    """
    from vp_access import caller_identity

    assert caller_identity({"claims": {"email": "e@x"}, "username": "u@x"}) == "e@x"
    assert caller_identity({"claims": {"cognito:username": "c@x"}, "username": "u@x"}) == "c@x"
    assert caller_identity({"claims": {}, "username": "u@x"}) == "u@x"
    assert caller_identity({}) == ""
