"""Minimal Lambda MicroVMs client built on botocore's SigV4 signer.

WHY THIS EXISTS
---------------
`boto3.client("lambda-microvms")` raises UnknownServiceError on the Lambda
python3.12 runtime: the service launched 2026-06 and the runtime's bundled
botocore has no service model for it. The obvious fixes (a boto3 layer, or real
deployment packages) both add build machinery to a stack that currently has
none — the VP template is plain CloudFormation with no SAM transform and no
build step, and its Lambdas are inline `Code: ZipFile`.

None of that is necessary. The API is ordinary REST-JSON on the *standard Lambda
endpoint*: its service model declares `endpointPrefix: lambda` and
`signingName: lambda`. So the calls can be made with `urllib` plus botocore's
SigV4 signer, both of which every Lambda runtime already has.

Verified against the live service (2026-08-08): GetMicrovmImage returned HTTP 200
with plain botocore SigV4 and no additional dependencies.

If a future runtime bundles a boto3 that knows `lambda-microvms`, this module can
be deleted and replaced with `boto3.client("lambda-microvms")` — the method names
and payloads here mirror the SDK's deliberately.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

API_VERSION = "2025-09-09"
# Both the endpoint prefix and the SigV4 signing name are "lambda", not
# "lambda-microvms" — taken from the service model, not guessed.
SIGNING_NAME = "lambda"


class MicrovmError(RuntimeError):
    """An API call failed. `code` carries the service's error type when present."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class MicrovmClient:
    """Thin wrapper over the Lambda MicroVMs REST API."""

    def __init__(self, region: str | None = None, session: boto3.Session | None = None) -> None:
        self.region = region or os.environ.get("AWS_REGION") or "us-east-1"
        self._session = session or boto3.Session()
        self._endpoint = f"https://lambda.{self.region}.amazonaws.com"

    # -- transport ---------------------------------------------------------

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._endpoint}{path}"
        payload = json.dumps(body).encode() if body is not None else None

        credentials = self._session.get_credentials()
        if credentials is None:
            raise MicrovmError("No AWS credentials available")
        frozen = credentials.get_frozen_credentials()

        signed = AWSRequest(
            method=method,
            url=url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(frozen, SIGNING_NAME, self.region).add_auth(signed)

        request = urllib.request.Request(
            url, data=payload, method=method, headers=dict(signed.headers)
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            code = ""
            try:
                parsed = json.loads(detail)
                code = parsed.get("__type") or parsed.get("code") or ""
                detail = parsed.get("message") or parsed.get("Message") or detail
            except (ValueError, AttributeError):
                pass
            raise MicrovmError(
                f"{method} {path} failed ({exc.code}): {detail}", status=exc.code, code=code
            ) from exc
        except urllib.error.URLError as exc:
            raise MicrovmError(f"{method} {path} failed: {exc.reason}") from exc

    # -- operations --------------------------------------------------------

    def run_microvm(self, **kwargs) -> dict:
        """RunMicrovm. Accepts the same keys as the SDK (camelCase)."""
        body = {k: v for k, v in kwargs.items() if v is not None}
        return self._call("POST", f"/{API_VERSION}/microvms", body)

    def get_microvm(self, microvm_identifier: str) -> dict:
        ident = urllib.parse.quote(microvm_identifier, safe="")
        return self._call("GET", f"/{API_VERSION}/microvms/{ident}")

    def terminate_microvm(self, microvm_identifier: str) -> dict:
        ident = urllib.parse.quote(microvm_identifier, safe="")
        return self._call("DELETE", f"/{API_VERSION}/microvms/{ident}")

    def create_microvm_auth_token(
        self, microvm_identifier: str, expiration_in_minutes: int, allowed_ports: list[dict]
    ) -> dict:
        ident = urllib.parse.quote(microvm_identifier, safe="")
        return self._call(
            "POST",
            f"/{API_VERSION}/microvms/{ident}/auth-token",
            {
                "expirationInMinutes": expiration_in_minutes,
                "allowedPorts": allowed_ports,
            },
        )

    def get_microvm_image(self, image_identifier: str) -> dict:
        ident = urllib.parse.quote(image_identifier, safe="")
        return self._call("GET", f"/{API_VERSION}/microvm-images/{ident}")
