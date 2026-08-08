"""Unit tests for the SigV4-based Lambda MicroVMs client (no AWS calls).

The client exists because `boto3.client("lambda-microvms")` raises
UnknownServiceError on the Lambda python3.12 runtime — its bundled botocore has
no service model for a service that launched 2026-06. These tests pin the wire
contract (paths, methods, signing name) against the published service model, so
a hand-rolled client cannot silently drift from the real API.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

CLIENT_PATH = (
    Path(__file__).resolve().parents[1]
    / "lambda_functions"
    / "microvm_launcher"
    / "microvm_client.py"
)


@pytest.fixture(scope="module")
def module():
    spec = importlib.util.spec_from_file_location("microvm_client", CLIENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["microvm_client"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_signing_name_and_api_version(module) -> None:
    """Both come from the service model, not guesswork.

    endpointPrefix and signingName are "lambda" — NOT "lambda-microvms" — which
    is what makes calling the API with a plain SigV4 signer possible.
    """
    assert module.SIGNING_NAME == "lambda"
    assert module.API_VERSION == "2025-09-09"


def test_endpoint_uses_the_standard_lambda_host(module) -> None:
    client = module.MicrovmClient(region="eu-west-1", session=mock.MagicMock())
    assert client._endpoint == "https://lambda.eu-west-1.amazonaws.com"


@pytest.mark.parametrize(
    ("method_name", "args", "http_method", "path"),
    [
        ("get_microvm", ("mvm-1",), "GET", "/2025-09-09/microvms/mvm-1"),
        ("terminate_microvm", ("mvm-1",), "DELETE", "/2025-09-09/microvms/mvm-1"),
        (
            "get_microvm_image",
            ("img-1",),
            "GET",
            "/2025-09-09/microvm-images/img-1",
        ),
    ],
)
def test_request_paths_match_the_service_model(
    module, method_name, args, http_method, path
) -> None:
    client = module.MicrovmClient(region="us-west-2", session=mock.MagicMock())
    with mock.patch.object(client, "_call", return_value={}) as called:
        getattr(client, method_name)(*args)
    called.assert_called_once()
    assert called.call_args[0][0] == http_method
    assert called.call_args[0][1] == path


def test_run_microvm_posts_to_the_collection_path(module) -> None:
    client = module.MicrovmClient(region="us-west-2", session=mock.MagicMock())
    with mock.patch.object(client, "_call", return_value={"microvmId": "m"}) as called:
        client.run_microvm(imageIdentifier="img", executionRoleArn="role", clientToken=None)
    method, path, body = called.call_args[0]
    assert (method, path) == ("POST", "/2025-09-09/microvms")
    # None values are dropped rather than sent as JSON null, which the API rejects.
    assert body == {"imageIdentifier": "img", "executionRoleArn": "role"}


def test_auth_token_path_and_body(module) -> None:
    client = module.MicrovmClient(region="us-west-2", session=mock.MagicMock())
    with mock.patch.object(client, "_call", return_value={}) as called:
        client.create_microvm_auth_token("mvm-9", 60, [{"port": 5901}])
    method, path, body = called.call_args[0]
    assert method == "POST"
    assert path == "/2025-09-09/microvms/mvm-9/auth-token"
    assert body == {"expirationInMinutes": 60, "allowedPorts": [{"port": 5901}]}


def test_identifiers_are_url_encoded(module) -> None:
    """MicroVM images are addressed by ARN, whose colons must be escaped."""
    client = module.MicrovmClient(region="us-west-2", session=mock.MagicMock())
    arn = "arn:aws:lambda:us-west-2:123456789012:microvm-image:my-image"
    with mock.patch.object(client, "_call", return_value={}) as called:
        client.get_microvm_image(arn)
    path = called.call_args[0][1]
    assert ":" not in path.split("/microvm-images/")[1], "ARN colons must be percent-encoded"
    assert "%3A" in path


def test_http_error_is_wrapped_with_status_and_message(module) -> None:
    import urllib.error

    client = module.MicrovmClient(region="us-west-2", session=mock.MagicMock())
    body = json.dumps({"message": "quota exceeded", "__type": "ServiceQuotaExceededException"})
    err = urllib.error.HTTPError(
        "https://x", 402, "Payment Required", {}, __import__("io").BytesIO(body.encode())
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with mock.patch.object(module.SigV4Auth, "add_auth"):
            with pytest.raises(module.MicrovmError) as excinfo:
                client.get_microvm("mvm-1")
    # The caller needs the service's own words to show a useful failure reason.
    assert excinfo.value.status == 402
    assert excinfo.value.code == "ServiceQuotaExceededException"
    assert "quota exceeded" in str(excinfo.value)


def test_missing_credentials_raises_a_clear_error(module) -> None:
    session = mock.MagicMock()
    session.get_credentials.return_value = None
    client = module.MicrovmClient(region="us-west-2", session=session)
    with pytest.raises(module.MicrovmError, match="No AWS credentials"):
        client.get_microvm("mvm-1")


def test_empty_response_body_is_tolerated(module) -> None:
    """DELETE returns 200 with no body."""
    client = module.MicrovmClient(region="us-west-2", session=mock.MagicMock())
    response = mock.MagicMock()
    response.read.return_value = b""
    response.__enter__ = lambda self: self
    response.__exit__ = lambda *a: None
    with mock.patch("urllib.request.urlopen", return_value=response):
        with mock.patch.object(module.SigV4Auth, "add_auth"):
            assert client.terminate_microvm("mvm-1") == {}


def test_client_has_no_third_party_imports(module) -> None:
    """Only boto3/botocore and the stdlib — the whole point of this approach.

    If this ever needs a real dependency, the Lambdas need a layer or a built
    package, which is exactly the complexity this avoids.
    """
    source = CLIENT_PATH.read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    }
    allowed = {"json", "os", "urllib", "boto3", "botocore", "__future__"}
    assert imports <= allowed, f"unexpected imports: {imports - allowed}"
