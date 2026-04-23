# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Stack operations namespace — thin wrapper over _core.stack."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Callable

from lma_sdk._core.stack import StackManager
from lma_sdk.models.stack import (
    FailureAnalysis,
    LogEntry,
    StackDeleteResult,
    StackDeployResult,
    StackEvent,
    StackMonitorResult,
    StackOperationInProgress,
    StackOutput,
    StackStatusResult,
)

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient


class StackOperations:
    """Stack operations accessible via ``client.stack``."""

    def __init__(self, client: LMAClient) -> None:
        self._manager = StackManager(client)

    def status(self, stack_name: str | None = None) -> StackStatusResult:
        """Get stack status.

        Args:
            stack_name: Stack name (defaults to client's stack_name).

        Returns:
            StackStatusResult with stack info.

        Example::

            result = client.stack.status()
            if result.exists:
                print(f"Status: {result.stack.status}")
        """
        return self._manager.status(stack_name)

    def exists(self, stack_name: str | None = None) -> bool:
        """Check if the stack exists.

        Args:
            stack_name: Stack name override.

        Returns:
            True if the stack exists.

        Example::

            if client.stack.exists():
                print("Stack exists")
        """
        return self._manager.exists(stack_name)

    def check_in_progress(self, stack_name: str | None = None) -> StackOperationInProgress | None:
        """Check if the stack has an operation in progress.

        Args:
            stack_name: Stack name override.

        Returns:
            StackOperationInProgress if busy, None otherwise.

        Example::

            in_progress = client.stack.check_in_progress()
            if in_progress:
                print(f"Operation {in_progress.operation} is in progress")
        """
        return self._manager.check_in_progress(stack_name)

    def outputs(self, stack_name: str | None = None) -> dict[str, StackOutput]:
        """Get stack outputs.

        Args:
            stack_name: Stack name override.

        Returns:
            Dict of output key to StackOutput.

        Example::

            outputs = client.stack.outputs()
            print(outputs["CloudFrontEndpoint"].value)
        """
        return self._manager.outputs(stack_name)

    def deploy(
        self,
        template_url: str | None = None,
        template_file: str | None = None,
        stack_name: str | None = None,
        parameters: dict[str, str] | None = None,
        capabilities: list[str] | None = None,
        wait: bool = True,
        timeout_minutes: int = 120,
        no_rollback: bool = False,
        role_arn: str | None = None,
    ) -> StackDeployResult:
        """Deploy or update the LMA stack.

        Args:
            template_url: S3 URL for the CloudFormation template.
            template_file: Local file path (alternative to template_url).
            stack_name: Stack name (defaults to client's).
            parameters: CloudFormation parameter overrides.
            capabilities: IAM capabilities (defaults to NAMED_IAM + AUTO_EXPAND).
            wait: Wait for completion (default True).
            timeout_minutes: Max wait time (default 120).
            no_rollback: Disable rollback on creation failure.
            role_arn: CloudFormation service role ARN.

        Returns:
            StackDeployResult with outcome.

        Example::

            result = client.stack.deploy(
                template_url="https://s3.us-east-1.amazonaws.com/...",
                parameters={"AdminEmail": "admin@example.com"},
            )
        """
        return self._manager.deploy(
            template_url=template_url,
            template_file=template_file,
            stack_name=stack_name,
            parameters=parameters,
            capabilities=capabilities,
            wait=wait,
            timeout_minutes=timeout_minutes,
            no_rollback=no_rollback,
            role_arn=role_arn,
        )

    def monitor(
        self,
        stack_name: str | None = None,
        operation: str = "UPDATE",
        poll_interval: int = 10,
        timeout_seconds: int = 7200,
        event_callback: Callable[[StackEvent], None] | None = None,
    ) -> StackMonitorResult:
        """Monitor a stack operation until completion.

        Polls the stack and optionally streams events via a callback.

        Args:
            stack_name: Stack name override.
            operation: Operation type: CREATE, UPDATE, or DELETE.
            poll_interval: Seconds between polls (default 10).
            timeout_seconds: Max wait time in seconds (default 7200).
            event_callback: Optional callback for each new stack event.

        Returns:
            StackMonitorResult with final outcome.

        Example::

            result = client.stack.monitor(operation="CREATE")
            if result.success:
                print(f"Stack created! Outputs: {result.outputs}")
        """
        return self._manager.monitor(
            stack_name=stack_name,
            operation=operation,
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
            event_callback=event_callback,
        )

    def get_stack_events(self, stack_name: str | None = None, limit: int = 20) -> list[StackEvent]:
        """Get recent CloudFormation stack events.

        Args:
            stack_name: Stack name override.
            limit: Max events to return (default 20).

        Returns:
            List of StackEvent objects, newest first.
        """
        return self._manager.get_stack_events(stack_name, limit)

    def get_failure_analysis(
        self,
        stack_name: str | None = None,
        deploy_start_time: datetime | None = None,
    ) -> FailureAnalysis:
        """Analyze a deployment failure with root cause identification.

        Recursively collects failed events from the main stack and all nested
        stacks, classifies root causes vs. cascade failures, and returns
        structured analysis.

        Args:
            stack_name: Stack name override.
            deploy_start_time: UTC timestamp when deployment started.
                Only events after this time are analyzed.

        Returns:
            FailureAnalysis with root_causes and all_failures.

        Example::

            analysis = client.stack.get_failure_analysis()
            for cause in analysis.root_causes:
                print(f"{cause.stack_path} → {cause.resource}: {cause.reason}")
        """
        return self._manager.get_failure_analysis(stack_name, deploy_start_time)

    def delete(self, stack_name: str | None = None, wait: bool = True) -> StackDeleteResult:
        """Delete the LMA stack.

        Args:
            stack_name: Stack name override.
            wait: Wait for deletion (default True).

        Returns:
            StackDeleteResult with outcome.
        """
        return self._manager.delete(stack_name, wait)

    def get_log_groups(self, stack_name: str | None = None) -> list[str]:
        """List CloudWatch log groups for the stack.

        Args:
            stack_name: Stack name override.

        Returns:
            List of log group names.
        """
        return self._manager.get_log_groups(stack_name)

    def tail_logs(
        self,
        log_group: str,
        since_minutes: int = 15,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Get recent log entries from a CloudWatch log group.

        Args:
            log_group: Log group name.
            since_minutes: How far back to look (default 15).
            limit: Max entries to return (default 100).

        Returns:
            List of LogEntry objects.
        """
        return self._manager.tail_logs(log_group, since_minutes, limit)
