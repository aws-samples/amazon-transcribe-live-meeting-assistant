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
    outputs: dict[str, str] = Field(default_factory=dict)
