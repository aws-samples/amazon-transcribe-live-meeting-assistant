# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""LMA SDK client — main entry point for all LMA operations.

Usage::

    from lma_sdk import LMAClient

    client = LMAClient(stack_name="LMA", region="us-east-1")

    # Stack operations
    status = client.stack.status()
    outputs = client.stack.outputs()

    # Publish operations
    result = client.publish.publish(
        bucket_basename="my-lma",
        prefix="lma",
        region="us-east-1",
    )
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3

from lma_sdk.exceptions import LMAConfigurationError

logger = logging.getLogger(__name__)


class LMAClient:
    """Main client for the LMA SDK.

    Provides access to operation namespaces:

    - ``client.stack`` — CloudFormation stack operations (status, deploy, delete, logs)
    - ``client.publish`` — Build and publish artifacts to S3

    Args:
        stack_name: CloudFormation stack name (default: env ``LMA_STACK_NAME`` or ``"LMA"``).
        region: AWS region (default: env ``AWS_DEFAULT_REGION`` or ``"us-east-1"``).
        profile: AWS CLI profile name (default: env ``AWS_PROFILE``).
        session: Pre-configured boto3 session (overrides profile/region).

    Example::

        client = LMAClient(stack_name="LMA", region="us-east-1")
        print(client.stack.status())
    """

    def __init__(
        self,
        stack_name: str | None = None,
        region: str | None = None,
        profile: str | None = None,
        session: boto3.Session | None = None,
    ) -> None:
        if stack_name is not None:
            self.stack_name = stack_name
        else:
            self.stack_name = os.environ.get("LMA_STACK_NAME", "LMA")

        if session:
            self._session = session
        else:
            session_kwargs: dict[str, Any] = {}
            if region:
                session_kwargs["region_name"] = region
            elif os.environ.get("AWS_DEFAULT_REGION"):
                session_kwargs["region_name"] = os.environ["AWS_DEFAULT_REGION"]
            # Otherwise let boto3 determine region from AWS CLI profile config
            if profile or os.environ.get("AWS_PROFILE"):
                session_kwargs["profile_name"] = profile or os.environ.get("AWS_PROFILE")
            self._session = boto3.Session(**session_kwargs)

        # Resolve region from the session (handles profile config, env vars, etc.)
        self.region = self._session.region_name or "us-east-1"

        # Lazily initialised operation namespaces
        self._stack_ops = None
        self._publish_ops = None

        logger.debug(
            "LMAClient initialised (stack=%s, region=%s)",
            self.stack_name,
            self.region,
        )

    # ── Properties ────────────────────────────────────────────

    @property
    def session(self) -> boto3.Session:
        """The boto3 session used for AWS API calls."""
        return self._session

    # ── Operation namespaces (lazy-loaded) ────────────────────

    @property
    def stack(self):
        """Stack operations (status, deploy, delete, logs).

        Returns:
            StackOperations instance.
        """
        if self._stack_ops is None:
            from lma_sdk.operations.stack import StackOperations

            self._stack_ops = StackOperations(self)
        return self._stack_ops

    @property
    def publish(self):
        """Publish operations (build and upload artifacts to S3).

        Returns:
            PublishOperations instance.
        """
        if self._publish_ops is None:
            from lma_sdk.operations.publish import PublishOperations

            self._publish_ops = PublishOperations(self)
        return self._publish_ops

    # ── Helpers ───────────────────────────────────────────────

    def _require_stack(self) -> str:
        """Ensure stack_name is set, raising if not."""
        if not self.stack_name:
            raise LMAConfigurationError(
                "No stack name configured. "
                "Set via LMAClient(stack_name=...) or LMA_STACK_NAME env var."
            )
        return self.stack_name

    def __repr__(self) -> str:
        return f"LMAClient(stack_name={self.stack_name!r}, region={self.region!r})"
