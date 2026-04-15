# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Publish operations namespace — thin wrapper over _core.publish."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from lma_sdk._core.publish import (
    STACK_NAMES,
    Publisher,
    check_prerequisites,
)
from lma_sdk.models.publish import PublishConfig, PublishResult

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient


class PublishOperations:
    """Publish operations accessible via ``client.publish``."""

    def __init__(self, client: LMAClient) -> None:
        self._publisher = Publisher(client)

    @staticmethod
    def available_stacks() -> list[str]:
        """Return list of publishable stack names.

        Returns:
            List of stack name strings.

        Example::

            for name in client.publish.available_stacks():
                print(name)
        """
        return list(STACK_NAMES)

    @staticmethod
    def check_prerequisites() -> list[str]:
        """Check if all publish prerequisites are met.

        Returns:
            List of error messages (empty if all OK).

        Example::

            errors = client.publish.check_prerequisites()
            if errors:
                for e in errors:
                    print(f"Missing: {e}")
        """
        return check_prerequisites()

    def publish(
        self,
        bucket_basename: str,
        prefix: str,
        region: str,
        public: bool = False,
        project_dir: str = ".",
        version: str = "",
        stacks: list[str] | None = None,
        force: bool = False,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> PublishResult:
        """Publish LMA artifacts to S3.

        This is the Python equivalent of ``publish.sh``. It builds and uploads
        all stack artifacts to S3, ready for CloudFormation deployment.

        Args:
            bucket_basename: S3 bucket base name (region is appended).
            prefix: S3 key prefix for artifacts.
            region: AWS region.
            public: Set public-read ACLs on artifacts.
            project_dir: Path to LMA project root (default: cwd).
            version: Version override (default: read from VERSION file).
            stacks: List of specific stacks to publish (default: all).
            force: Skip change detection and publish all.
            progress_callback: Optional callback(stack_name, message).

        Returns:
            PublishResult with per-stack outcomes and deployment URLs.

        Example::

            result = client.publish.publish(
                bucket_basename="my-lma-artifacts",
                prefix="lma",
                region="us-east-1",
            )
            print(f"Template URL: {result.template_url}")
            print(f"Console URL: {result.console_url}")
        """
        config = PublishConfig(
            bucket_basename=bucket_basename,
            prefix=prefix,
            region=region,
            public=public,
            project_dir=project_dir,
            version=version,
            stacks=stacks,
            force=force,
        )
        return self._publisher.publish(config, progress_callback)
