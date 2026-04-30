# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Stack-related data models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class StackOutput(BaseModel):
    """A single CloudFormation stack output."""

    key: str
    value: str
    description: str = ""


class StackResource(BaseModel):
    """A CloudFormation stack resource."""

    logical_id: str
    physical_id: str = ""
    resource_type: str = ""
    status: str = ""


class StackInfo(BaseModel):
    """CloudFormation stack information."""

    stack_name: str
    stack_id: str = ""
    status: str = ""
    status_reason: str = ""
    creation_time: datetime | None = None
    last_updated_time: datetime | None = None
    outputs: list[StackOutput] = Field(default_factory=list)
    parameters: dict[str, str] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)


class StackStatusResult(BaseModel):
    """Result of a stack status query."""

    success: bool = True
    stack: StackInfo | None = None
    exists: bool = True
    message: str = ""


class StackDeployResult(BaseModel):
    """Result of a stack deploy operation."""

    success: bool = True
    stack_name: str = ""
    stack_id: str = ""
    status: str = ""
    message: str = ""
    template_url: str = ""
    console_url: str = ""
    outputs: list[StackOutput] = Field(default_factory=list)
    operation: str = ""


class StackDeleteResult(BaseModel):
    """Result of a stack delete operation."""

    success: bool = True
    stack_name: str = ""
    message: str = ""


class LogEntry(BaseModel):
    """A single log entry."""

    timestamp: datetime
    message: str
    log_stream: str = ""
    log_group: str = ""


class StackEvent(BaseModel):
    """A single CloudFormation stack event."""

    timestamp: datetime
    logical_resource_id: str = ""
    resource_type: str = ""
    resource_status: str = ""
    resource_status_reason: str = ""
    stack_name: str = ""


class FailureCause(BaseModel):
    """A single root-cause failure from a CloudFormation deployment."""

    resource: str = Field(description="CloudFormation logical resource ID")
    resource_type: str = Field(default="", description="CloudFormation resource type")
    reason: str = Field(description="CloudFormation failure reason string")
    status: str = Field(description="Resource status (e.g. CREATE_FAILED)")
    physical_id: str = Field(default="", description="Physical resource ID if available")
    stack: str = Field(description="Stack name containing this failure")
    stack_path: str = Field(
        default="",
        description="Nested stack path (e.g. 'NestedStack1 → NestedStack2')",
    )
    is_cascade: bool = Field(
        default=False,
        description="True if this failure was caused by another failure (not a root cause)",
    )


class FailureAnalysis(BaseModel):
    """Complete failure analysis for a CloudFormation deployment."""

    stack_name: str = Field(description="Top-level stack name")
    root_causes: list[FailureCause] = Field(
        default_factory=list,
        description="Actual root cause failures (excludes cascades and nested wrappers)",
    )
    all_failures: list[FailureCause] = Field(
        default_factory=list,
        description="All failed events across main and nested stacks",
    )

    @property
    def cascade_count(self) -> int:
        """Number of cascade/secondary failures (not root causes)."""
        return sum(1 for f in self.all_failures if f.is_cascade)


class StackOperationInProgress(BaseModel):
    """Describes an in-progress stack operation."""

    operation: str  # CREATE, UPDATE, DELETE
    status: str  # The current stack status string


class StackMonitorResult(BaseModel):
    """Result of monitoring a CloudFormation stack operation to completion."""

    success: bool = False
    stack_name: str = ""
    operation: str = ""
    status: str = ""
    error: str = ""
    deploy_start_time: datetime | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
