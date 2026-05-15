# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""AppSync GraphQL operations with SigV4 signing.

This namespace lets LMA SDK callers invoke any GraphQL operation on the LMA
AppSync API using the caller's AWS credentials (IAM auth), which works for
every field decorated with ``@aws_iam`` in ``schema.graphql`` — including
the full Virtual Participant CRUD surface.

Usage::

    result = client.appsync.graphql(
        query="mutation Create($i: CreateVirtualParticipantInput!) "
              "{ createVirtualParticipant(input: $i) { id status } }",
        variables={"i": {...}},
    )
    vp_id = result["createVirtualParticipant"]["id"]
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import botocore.httpsession
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from lma_sdk.exceptions import (
    LMAAppSyncError,
    LMAConfigurationError,
    LMAResourceNotFoundError,
)

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

logger = logging.getLogger(__name__)


class AppSyncOperations:
    """SigV4-signed GraphQL helper available as ``client.appsync``."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client
        self._graphql_url: str | None = None

    # ── URL resolution ────────────────────────────────────────

    @property
    def graphql_url(self) -> str:
        """Resolve and cache the LMA AppSync GraphQL URL.

        Looks up the CloudFormation outputs of the configured stack and
        searches for any of the known aliases:

        - ``GraphqlApiUrl`` / ``AppSyncGraphqlUrl`` / ``GraphQLApiURL``
        - Inside the space-separated ``LocalUITestingEnv`` output
          (``VITE_APPSYNC_GRAPHQL_URL=<url>``) — this is the most
          reliable in current LMA versions.
        """
        if self._graphql_url:
            return self._graphql_url

        try:
            outputs = self._client.stack.outputs()
        except LMAResourceNotFoundError as err:
            raise LMAConfigurationError(
                f"Could not resolve AppSync URL: {err}"
            ) from err

        url = _extract_appsync_url({k: o.value for k, o in outputs.items()})
        if not url:
            raise LMAConfigurationError(
                "Could not determine AppSync GraphQL URL from stack outputs. "
                "Expected one of: GraphqlApiUrl, AppSyncGraphqlUrl, or "
                "LocalUITestingEnv with VITE_APPSYNC_GRAPHQL_URL=<url>."
            )
        self._graphql_url = url
        return url

    # ── Core call ─────────────────────────────────────────────

    def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        """Execute a GraphQL query or mutation against the LMA AppSync API.

        The request is signed with SigV4 using the credentials on
        ``client.session``. Service name is always ``appsync``.

        Args:
            query: GraphQL document (query/mutation/subscription string).
            variables: Dict of GraphQL variables.
            operation_name: Optional operation name (for multi-op documents).
            timeout_s: HTTP read timeout.

        Returns:
            The ``data`` portion of the GraphQL response (never ``None``).

        Raises:
            LMAAppSyncError: The HTTP call succeeded but the response body
                contained ``errors``, or a non-2xx response was received.
            LMAConfigurationError: The AppSync URL could not be resolved or
                credentials are missing.
        """
        url = self.graphql_url
        body = {"query": query}
        if variables is not None:
            body["variables"] = variables
        if operation_name:
            body["operationName"] = operation_name
        data_bytes = json.dumps(body).encode("utf-8")

        creds = self._client.session.get_credentials()
        if creds is None:
            raise LMAConfigurationError(
                "No AWS credentials available. Configure AWS_PROFILE or "
                "environment credentials before calling AppSync."
            )
        frozen = creds.get_frozen_credentials()

        parsed = urlparse(url)
        region = self._client.region
        if parsed.hostname and ".amazonaws.com" in parsed.hostname:
            # Derive region from the host to avoid mismatch when caller's
            # default region differs from the LMA stack region.
            parts = parsed.hostname.split(".")
            if len(parts) >= 3:
                region = parts[-3]

        request = AWSRequest(
            method="POST",
            url=url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Host": parsed.hostname or "",
            },
        )
        SigV4Auth(frozen, "appsync", region).add_auth(request)
        prepared = request.prepare()

        # Use botocore's HTTP client (bundled with boto3) so we don't add
        # a hard dependency on urllib3/requests. The underlying session
        # pool is managed by botocore.
        endpoint = botocore.httpsession.URLLib3Session(
            timeout=timeout_s,
        )
        logger.debug("AppSync POST %s (op=%s)", url, operation_name or "<inline>")
        response = endpoint.send(prepared)

        status = response.status_code
        text = response.text or ""
        if status >= 400:
            raise LMAAppSyncError(
                f"AppSync HTTP {status}: {text}",
                details={"status": status, "body": text, "url": url},
            )

        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError as err:
            raise LMAAppSyncError(
                f"AppSync returned non-JSON response: {text[:500]}"
            ) from err

        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            messages = "; ".join(
                e.get("message", "unknown") if isinstance(e, dict) else str(e)
                for e in errors
            )
            raise LMAAppSyncError(
                f"AppSync GraphQL error: {messages}",
                details={"errors": errors, "url": url},
            )

        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}


# ── Helpers ───────────────────────────────────────────────────

_APPSYNC_ALIASES = (
    "GraphqlApiUrl",
    "AppSyncGraphqlUrl",
    "GraphQLApiURL",
    "GraphQLApiUrl",
)


def _extract_appsync_url(raw_outputs: dict[str, str]) -> str | None:
    """Find the AppSync GraphQL URL in a dict of CFN outputs."""
    for key in _APPSYNC_ALIASES:
        val = raw_outputs.get(key)
        if val:
            return val

    local_env = raw_outputs.get("LocalUITestingEnv") or ""
    for pair in local_env.split():
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k == "VITE_APPSYNC_GRAPHQL_URL" and v.strip():
            return v.strip()
    return None
