# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Core stack operations — CloudFormation interactions."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

import botocore.exceptions

from lma_sdk.exceptions import LMAStackError, LMAResourceNotFoundError
from lma_sdk.models.stack import (
    FailureAnalysis,
    FailureCause,
    LogEntry,
    StackDeleteResult,
    StackDeployResult,
    StackEvent,
    StackInfo,
    StackMonitorResult,
    StackOperationInProgress,
    StackOutput,
    StackStatusResult,
)

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

logger = logging.getLogger(__name__)

# Terminal states for stack operations
COMPLETE_STATES = {
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "DELETE_COMPLETE",
    "IMPORT_COMPLETE",
}
FAILED_STATES = {
    "CREATE_FAILED",
    "DELETE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_FAILED",
    "IMPORT_ROLLBACK_COMPLETE",
    "IMPORT_ROLLBACK_FAILED",
}
ROLLBACK_STATES = {
    "ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_ROLLBACK_COMPLETE",
}

# Maps in-progress statuses to their operation type
IN_PROGRESS_STATUS_MAP = {
    "CREATE_IN_PROGRESS": "CREATE",
    "UPDATE_IN_PROGRESS": "UPDATE",
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS": "UPDATE",
    "UPDATE_ROLLBACK_IN_PROGRESS": "UPDATE",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS": "UPDATE",
    "DELETE_IN_PROGRESS": "DELETE",
    "ROLLBACK_IN_PROGRESS": "CREATE",
    "REVIEW_IN_PROGRESS": "CREATE",
}


class StackManager:
    """Manages CloudFormation stack operations for LMA."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client

    @property
    def _cfn(self):
        return self._client.session.client("cloudformation", region_name=self._client.region)

    @property
    def _logs_client(self):
        return self._client.session.client("logs", region_name=self._client.region)

    def status(self, stack_name: str | None = None) -> StackStatusResult:
        """Get the current status of the LMA stack.

        Args:
            stack_name: Stack name override (defaults to client's stack_name).

        Returns:
            StackStatusResult with stack information.
        """
        name = stack_name or self._client.stack_name
        if not name:
            return StackStatusResult(
                success=False,
                exists=False,
                message="No stack name specified. Use --stack-name or set LMA_STACK_NAME.",
            )
        try:
            response = self._cfn.describe_stacks(StackName=name)
            stacks = response.get("Stacks", [])
            if not stacks:
                return StackStatusResult(success=True, exists=False, message=f"Stack '{name}' not found.")

            stack = stacks[0]
            outputs = [
                StackOutput(
                    key=o.get("OutputKey", ""),
                    value=o.get("OutputValue", ""),
                    description=o.get("Description", ""),
                )
                for o in stack.get("Outputs", [])
            ]
            parameters = {
                p["ParameterKey"]: p.get("ParameterValue", "")
                for p in stack.get("Parameters", [])
            }
            tags = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}

            info = StackInfo(
                stack_name=stack.get("StackName", name),
                stack_id=stack.get("StackId", ""),
                status=stack.get("StackStatus", ""),
                status_reason=stack.get("StackStatusReason", ""),
                creation_time=stack.get("CreationTime"),
                last_updated_time=stack.get("LastUpdatedTime"),
                outputs=outputs,
                parameters=parameters,
                tags=tags,
            )
            return StackStatusResult(success=True, stack=info, exists=True)

        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ValidationError" and "does not exist" in str(e):
                return StackStatusResult(success=True, exists=False, message=f"Stack '{name}' does not exist.")
            raise LMAStackError(f"Failed to describe stack '{name}': {e}") from e

    def exists(self, stack_name: str | None = None) -> bool:
        """Check if the stack exists.

        Args:
            stack_name: Stack name override.

        Returns:
            True if the stack exists, False otherwise.
        """
        result = self.status(stack_name)
        return result.exists

    def check_in_progress(self, stack_name: str | None = None) -> StackOperationInProgress | None:
        """Check if the stack has an operation in progress.

        Args:
            stack_name: Stack name override.

        Returns:
            StackOperationInProgress if an operation is in progress, None otherwise.
        """
        result = self.status(stack_name)
        if not result.exists or not result.stack:
            return None

        current_status = result.stack.status
        operation = IN_PROGRESS_STATUS_MAP.get(current_status)
        if operation:
            return StackOperationInProgress(operation=operation, status=current_status)
        return None

    def outputs(self, stack_name: str | None = None) -> dict[str, StackOutput]:
        """Get stack outputs as a dictionary keyed by output key.

        Args:
            stack_name: Stack name override.

        Returns:
            Dictionary of output key -> StackOutput.
        """
        result = self.status(stack_name)
        if not result.exists or not result.stack:
            raise LMAResourceNotFoundError(
                f"Stack '{stack_name or self._client.stack_name}' not found."
            )
        return {o.key: o for o in result.stack.outputs}

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
            template_url: S3 URL for the template.
            template_file: Local path to template file.
            stack_name: Stack name (defaults to client's).
            parameters: Parameter overrides.
            capabilities: IAM capabilities.
            wait: Whether to wait for completion.
            timeout_minutes: Max wait time.
            no_rollback: Disable rollback on creation failure.
            role_arn: CloudFormation service role ARN.

        Returns:
            StackDeployResult with deployment outcome.
        """
        name = stack_name or self._client.stack_name or "LMA"
        caps = capabilities or ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]

        try:
            # Check if stack exists
            existing = self.status(name)

            kwargs: dict[str, Any] = {
                "StackName": name,
                "Capabilities": caps,
            }
            if template_url:
                kwargs["TemplateURL"] = template_url
            elif template_file:
                with open(template_file) as f:
                    kwargs["TemplateBody"] = f.read()
            else:
                raise LMAStackError("Either template_url or template_file is required.")

            # Build parameter list based on whether stack exists
            if existing.exists:
                # For UPDATE: Use UsePreviousValue for parameters not explicitly provided.
                # This avoids "must have values" errors for required parameters like AdminEmail.
                param_list = self._build_update_parameters(kwargs, parameters or {})
            else:
                # For CREATE: Use only provided parameters, then auto-fill missing defaults.
                param_list = []
                if parameters:
                    for k, v in parameters.items():
                        param_list.append({"ParameterKey": k, "ParameterValue": v})
                param_list = self._fill_missing_parameter_defaults(kwargs, param_list)

            if param_list:
                kwargs["Parameters"] = param_list

            if role_arn:
                kwargs["RoleARN"] = role_arn

            if existing.exists:
                logger.info("Updating existing stack '%s'", name)
                try:
                    self._cfn.update_stack(**kwargs)
                    action = "UPDATE"
                except botocore.exceptions.ClientError as e:
                    if "No updates are to be performed" in str(e):
                        return StackDeployResult(
                            success=True,
                            stack_name=name,
                            status="UPDATE_COMPLETE",
                            message="No updates needed — stack is already up to date.",
                            operation="UPDATE",
                        )
                    raise
            else:
                logger.info("Creating new stack '%s'", name)
                if no_rollback:
                    kwargs["DisableRollback"] = True
                else:
                    kwargs["OnFailure"] = "ROLLBACK"
                if timeout_minutes:
                    kwargs["TimeoutInMinutes"] = timeout_minutes
                self._cfn.create_stack(**kwargs)
                action = "CREATE"

            if wait:
                return self._wait_for_stack(name, action)

            return StackDeployResult(
                success=True,
                stack_name=name,
                status=f"{action}_IN_PROGRESS",
                message=f"Stack {action.lower()} initiated. Use 'lma status' to monitor.",
                operation=action,
            )

        except botocore.exceptions.ClientError as e:
            raise LMAStackError(f"Deploy failed for '{name}': {e}") from e

    def _fill_missing_parameter_defaults(
        self,
        kwargs: dict[str, Any],
        param_list: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Use GetTemplateSummary to find parameters without defaults and
        auto-fill empty strings for any that the caller hasn't explicitly set.

        This fixes the CloudFormation API "must have values" error for
        parameters like VPC/Subnet that accept empty strings but have no
        Default in the template.
        """
        try:
            summary_kwargs: dict[str, Any] = {}
            if "TemplateURL" in kwargs:
                summary_kwargs["TemplateURL"] = kwargs["TemplateURL"]
            elif "TemplateBody" in kwargs:
                summary_kwargs["TemplateBody"] = kwargs["TemplateBody"]
            else:
                return param_list

            response = self._cfn.get_template_summary(**summary_kwargs)
            template_params = response.get("Parameters", [])

            # Build set of parameter keys the caller already provided
            provided_keys = {p["ParameterKey"] for p in param_list}

            for tp in template_params:
                key = tp.get("ParameterKey", "")
                has_default = "DefaultValue" in tp
                if not has_default and key not in provided_keys:
                    logger.debug(
                        "Auto-filling empty default for parameter '%s'", key
                    )
                    param_list.append(
                        {"ParameterKey": key, "ParameterValue": ""}
                    )
        except Exception as e:
            logger.debug(
                "Could not auto-fill parameter defaults: %s", e
            )

        return param_list

    def _build_update_parameters(
        self,
        kwargs: dict[str, Any],
        parameters: dict[str, str],
    ) -> list[dict[str, str | bool]]:
        """Build CloudFormation parameter list for stack updates.

        For existing stacks, uses ``UsePreviousValue: True`` for every
        parameter the caller did *not* explicitly provide.  This ensures
        required parameters like ``AdminEmail`` keep their current value
        without forcing the user to re-supply them on every update.

        Also detects deprecated parameters (present in the existing stack but
        absent from the new template) and drops them to avoid update errors.
        """
        # Get current parameters from the existing stack
        status = self.status(kwargs["StackName"])
        current_params: dict[str, str] = {}
        if status.exists and status.stack:
            current_params = status.stack.parameters or {}

        # Get valid parameter keys from the new template so we can detect
        # deprecated parameters that were removed between versions.
        valid_template_params = self._get_template_parameters(kwargs)

        deprecated_params: set[str] = set()
        if valid_template_params:
            deprecated_params = set(current_params.keys()) - valid_template_params
            if deprecated_params:
                logger.warning(
                    "Dropping deprecated parameters not in new template: %s",
                    deprecated_params,
                )

        param_list: list[dict[str, str | bool]] = []

        # First, handle all existing parameters
        for param_key in current_params:
            if param_key in deprecated_params:
                logger.info("Skipping deprecated parameter: %s", param_key)
                continue

            if param_key in parameters:
                # User provided a new value — use it
                param_list.append(
                    {"ParameterKey": param_key, "ParameterValue": parameters[param_key]}
                )
            else:
                # Keep previous value
                param_list.append(
                    {"ParameterKey": param_key, "UsePreviousValue": True}
                )

        # Then, add any *new* parameters not present in the current stack
        for param_key, param_value in parameters.items():
            if param_key not in current_params:
                param_list.append(
                    {"ParameterKey": param_key, "ParameterValue": param_value}
                )

        return param_list

    def _get_template_parameters(
        self,
        kwargs: dict[str, Any],
    ) -> set[str]:
        """Return the set of parameter keys defined in a CloudFormation template.

        Uses ``GetTemplateSummary`` to introspect the template (from either
        ``TemplateURL`` or ``TemplateBody`` in *kwargs*).  Returns an empty
        set on failure so callers can gracefully fall back.
        """
        try:
            summary_kwargs: dict[str, Any] = {}
            if "TemplateURL" in kwargs:
                summary_kwargs["TemplateURL"] = kwargs["TemplateURL"]
            elif "TemplateBody" in kwargs:
                summary_kwargs["TemplateBody"] = kwargs["TemplateBody"]
            else:
                return set()

            response = self._cfn.get_template_summary(**summary_kwargs)
            template_params = response.get("Parameters", [])
            param_keys = {
                tp.get("ParameterKey", "")
                for tp in template_params
                if tp.get("ParameterKey")
            }
            logger.debug("Template has %d parameters: %s", len(param_keys), param_keys)
            return param_keys
        except Exception as e:
            logger.debug("Could not get template parameters: %s", e)
            return set()

    def monitor(
        self,
        stack_name: str | None = None,
        operation: str = "UPDATE",
        poll_interval: int = 10,
        timeout_seconds: int = 7200,
        event_callback: Callable[[StackEvent], None] | None = None,
    ) -> StackMonitorResult:
        """Monitor a stack operation until it reaches a terminal state.

        Polls CloudFormation and optionally streams events via callback.

        Args:
            stack_name: Stack name override.
            operation: Operation type: CREATE, UPDATE, or DELETE.
            poll_interval: Seconds between polls (default 10).
            timeout_seconds: Max wait time in seconds (default 7200 = 2h).
            event_callback: Optional callback for each new stack event.

        Returns:
            StackMonitorResult with final outcome.
        """
        name = stack_name or self._client.stack_name
        if not name:
            raise LMAStackError("No stack name specified.")

        complete_statuses = {
            "CREATE": {"CREATE_COMPLETE"},
            "UPDATE": {"UPDATE_COMPLETE"},
            "DELETE": {"DELETE_COMPLETE"},
        }
        failure_statuses = {
            "CREATE": {"CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED"},
            "UPDATE": {"UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED", "UPDATE_FAILED"},
            "DELETE": {"DELETE_FAILED"},
        }

        success_states = complete_statuses.get(operation, set())
        fail_states = failure_statuses.get(operation, set())

        seen_event_ids: set[str] = set()
        start_time = time.time()
        deploy_start_time = datetime.now(timezone.utc)

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                return StackMonitorResult(
                    success=False,
                    stack_name=name,
                    operation=operation,
                    status="TIMEOUT",
                    error=f"Monitoring timed out after {timeout_seconds}s",
                )

            try:
                # Get recent events and stream new ones
                if event_callback:
                    events = self.get_stack_events(name, limit=20)
                    for event in reversed(events):  # oldest first
                        event_id = f"{event.timestamp.isoformat()}-{event.logical_resource_id}-{event.resource_status}"
                        if event_id not in seen_event_ids:
                            seen_event_ids.add(event_id)
                            event_callback(event)

                # Check current status
                result = self.status(name)

                # Handle DELETE where stack disappears
                if operation == "DELETE" and not result.exists:
                    return StackMonitorResult(
                        success=True,
                        stack_name=name,
                        operation=operation,
                        status="DELETE_COMPLETE",
                    )

                if result.exists and result.stack:
                    current_status = result.stack.status

                    if current_status in success_states:
                        outputs = {o.key: o.value for o in result.stack.outputs}
                        return StackMonitorResult(
                            success=True,
                            stack_name=name,
                            operation=operation,
                            status=current_status,
                            outputs=outputs,
                        )

                    if current_status in fail_states:
                        error_reason = result.stack.status_reason or current_status
                        return StackMonitorResult(
                            success=False,
                            stack_name=name,
                            operation=operation,
                            status=current_status,
                            error=error_reason,
                            deploy_start_time=deploy_start_time,
                        )

            except Exception as e:
                logger.debug("Error during monitoring poll: %s", e)

            time.sleep(poll_interval)

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
            stack_name: Stack name (defaults to client's stack_name).
            deploy_start_time: UTC timestamp when deployment started.
                Only events after this time are analyzed.

        Returns:
            FailureAnalysis with root_causes and all_failures.
        """
        name = stack_name or self._client.stack_name
        if not name:
            raise LMAStackError("No stack name specified.")

        raw = self._get_deployment_failure_analysis(name, deploy_start_time=deploy_start_time)

        def _to_cause(d: dict) -> FailureCause:
            return FailureCause(
                resource=d.get("resource", "Unknown"),
                resource_type=d.get("resource_type", ""),
                reason=d.get("reason", "Unknown"),
                status=d.get("status", ""),
                physical_id=d.get("physical_id", ""),
                stack=d.get("stack", name),
                stack_path=d.get("stack_path", ""),
                is_cascade=d.get("is_cascade", False),
            )

        return FailureAnalysis(
            stack_name=raw.get("stack_name", name),
            root_causes=[_to_cause(c) for c in raw.get("root_causes", [])],
            all_failures=[_to_cause(f) for f in raw.get("all_failures", [])],
        )

    def _get_deployment_failure_analysis(
        self,
        stack_name: str,
        _depth: int = 0,
        deploy_start_time: datetime | None = None,
    ) -> dict:
        """Recursively collect failed events from a stack and its nested stacks.

        Args:
            stack_name: Stack name or ARN.
            _depth: Internal recursion depth counter (max 5).
            deploy_start_time: Only events after this time are considered.

        Returns:
            Dict with root_causes, all_failures, and stack_name.
        """
        if _depth > 5:
            return {"root_causes": [], "all_failures": [], "stack_name": stack_name}

        all_failures: list[dict] = []

        try:
            paginator = self._cfn.get_paginator("describe_stack_events")
            for page in paginator.paginate(StackName=stack_name):
                for event in page.get("StackEvents", []):
                    status = event.get("ResourceStatus", "")
                    if "FAILED" not in status:
                        continue

                    if deploy_start_time:
                        event_time = event.get("Timestamp")
                        if event_time and event_time < deploy_start_time:
                            continue

                    reason = event.get("ResourceStatusReason", "")
                    resource_id = event.get("LogicalResourceId", "Unknown")
                    resource_type = event.get("ResourceType", "")
                    physical_id = event.get("PhysicalResourceId", "")

                    display_stack = stack_name
                    if "/" in str(stack_name):
                        try:
                            display_stack = str(stack_name).split("/")[1]
                        except IndexError:
                            pass

                    failure = {
                        "resource": resource_id,
                        "resource_type": resource_type,
                        "reason": reason,
                        "status": status,
                        "physical_id": physical_id,
                        "stack": display_stack,
                        "stack_path": "",
                        "is_nested_wrapper": False,
                        "is_cascade": False,
                    }

                    if reason and (
                        "Resource creation cancelled" in reason
                        or "resource creation Cancelled" in reason
                        or "Resource update cancelled" in reason
                    ):
                        failure["is_cascade"] = True

                    if (
                        resource_type == "AWS::CloudFormation::Stack"
                        and reason
                        and (
                            "was not successfully created" in reason
                            or "was not successfully updated" in reason
                        )
                    ):
                        failure["is_nested_wrapper"] = True

                        if physical_id:
                            nested = self._get_deployment_failure_analysis(
                                physical_id,
                                _depth=_depth + 1,
                                deploy_start_time=deploy_start_time,
                            )
                            for nested_failure in nested.get("all_failures", []):
                                if nested_failure.get("stack_path"):
                                    nested_failure["stack_path"] = (
                                        f"{resource_id} → {nested_failure['stack_path']}"
                                    )
                                else:
                                    nested_failure["stack_path"] = resource_id
                            all_failures.extend(nested.get("all_failures", []))

                    all_failures.append(failure)

        except Exception as e:
            logger.warning("Error getting stack events for %s: %s", stack_name, e)
            all_failures.append(
                {
                    "resource": "Unknown",
                    "resource_type": "",
                    "reason": f"Could not retrieve stack events: {e}",
                    "status": "UNKNOWN",
                    "physical_id": "",
                    "stack": str(stack_name),
                    "stack_path": "",
                    "is_nested_wrapper": False,
                    "is_cascade": False,
                }
            )

        root_causes = [
            f for f in all_failures if not f["is_cascade"] and not f["is_nested_wrapper"]
        ]

        return {
            "root_causes": root_causes,
            "all_failures": all_failures,
            "stack_name": stack_name,
        }

    def _wait_for_stack(
        self, stack_name: str, action: str, poll_interval: int = 15
    ) -> StackDeployResult:
        """Poll stack until terminal state is reached."""
        logger.info("Waiting for stack '%s' %s to complete...", stack_name, action)
        waiter_name = f"stack_{action.lower()}_complete"
        try:
            waiter = self._cfn.get_waiter(waiter_name)
            waiter.wait(
                StackName=stack_name,
                WaiterConfig={"Delay": poll_interval, "MaxAttempts": 480},
            )
        except botocore.exceptions.WaiterError:
            pass  # Fall through to check actual status

        result = self.status(stack_name)
        if result.stack:
            stack_status = result.stack.status
            region = self._client.region
            console_url = (
                f"https://{region}.console.aws.amazon.com/cloudformation/home"
                f"?region={region}#/stacks/stackinfo?stackId={result.stack.stack_id}"
            )
            success = stack_status in COMPLETE_STATES
            return StackDeployResult(
                success=success,
                stack_name=stack_name,
                stack_id=result.stack.stack_id,
                status=stack_status,
                message=(
                    result.stack.status_reason if not success
                    else f"Stack {action.lower()} completed successfully."
                ),
                console_url=console_url,
                outputs=result.stack.outputs,
                operation=action,
            )
        raise LMAStackError(f"Stack '{stack_name}' disappeared during {action}.")

    def get_stack_events(self, stack_name: str | None = None, limit: int = 20) -> list[StackEvent]:
        """Get recent CloudFormation stack events.

        Args:
            stack_name: Stack name override.
            limit: Max events to return (default 20).

        Returns:
            List of StackEvent objects, newest first.
        """
        name = stack_name or self._client.stack_name
        if not name:
            return []

        try:
            response = self._cfn.describe_stack_events(StackName=name)
            events_raw = response.get("StackEvents", [])[:limit]
            events = []
            for ev in events_raw:
                events.append(
                    StackEvent(
                        timestamp=ev.get("Timestamp", datetime.now(tz=timezone.utc)),
                        logical_resource_id=ev.get("LogicalResourceId", ""),
                        resource_type=ev.get("ResourceType", ""),
                        resource_status=ev.get("ResourceStatus", ""),
                        resource_status_reason=ev.get("ResourceStatusReason", ""),
                        stack_name=ev.get("StackName", name),
                    )
                )
            return events
        except botocore.exceptions.ClientError as err:
            logger.debug("Failed to get stack events for '%s': %s", name, err)
            return []

    def delete(self, stack_name: str | None = None, wait: bool = True) -> StackDeleteResult:
        """Delete the LMA stack.

        Args:
            stack_name: Stack name override.
            wait: Whether to wait for deletion.

        Returns:
            StackDeleteResult with outcome.
        """
        name = stack_name or self._client.stack_name
        if not name:
            raise LMAStackError("No stack name specified.")

        try:
            self._cfn.delete_stack(StackName=name)
            if wait:
                waiter = self._cfn.get_waiter("stack_delete_complete")
                waiter.wait(StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 480})

            return StackDeleteResult(
                success=True,
                stack_name=name,
                message=f"Stack '{name}' deleted successfully.",
            )
        except botocore.exceptions.ClientError as e:
            raise LMAStackError(f"Failed to delete stack '{name}': {e}") from e
        except botocore.exceptions.WaiterError as e:
            raise LMAStackError(f"Timeout waiting for stack '{name}' deletion: {e}") from e

    def get_log_groups(self, stack_name: str | None = None) -> list[str]:
        """Discover CloudWatch log groups for the stack and its nested stacks.

        Searches for log groups matching the stack name prefix, which includes
        groups from nested stacks (e.g. /LMA-Bob-AISTACK-xxx/lambda/...).

        Args:
            stack_name: Stack name override.

        Returns:
            List of log group names.
        """
        name = stack_name or self._client.stack_name
        if not name:
            return []

        # Use /{name}- to match nested stack log groups (e.g. /LMA-Bob-AISTACK-xxx/)
        # and /{name}/ for any direct log groups
        prefixes = [f"/{name}-", f"/{name}/"]
        log_groups = []
        seen = set()
        paginator = self._logs_client.get_paginator("describe_log_groups")
        for prefix in prefixes:
            for page in paginator.paginate(logGroupNamePrefix=prefix):
                for lg in page.get("logGroups", []):
                    lg_name = lg["logGroupName"]
                    if lg_name not in seen:
                        seen.add(lg_name)
                        log_groups.append(lg_name)
        return log_groups

    def tail_logs(
        self,
        log_group: str,
        since_minutes: int = 15,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Retrieve recent log events from a CloudWatch log group.

        Args:
            log_group: Log group name.
            since_minutes: How far back to look.
            limit: Max events to return.

        Returns:
            List of LogEntry objects.
        """
        start_time = int((time.time() - since_minutes * 60) * 1000)
        try:
            response = self._logs_client.filter_log_events(
                logGroupName=log_group,
                startTime=start_time,
                limit=limit,
                interleaved=True,
            )
            entries = []
            for event in response.get("events", []):
                entries.append(
                    LogEntry(
                        timestamp=datetime.fromtimestamp(
                            event["timestamp"] / 1000, tz=timezone.utc
                        ),
                        message=event.get("message", "").rstrip(),
                        log_stream=event.get("logStreamName", ""),
                        log_group=log_group,
                    )
                )
            return entries
        except botocore.exceptions.ClientError as e:
            raise LMAStackError(f"Failed to read logs from '{log_group}': {e}") from e
