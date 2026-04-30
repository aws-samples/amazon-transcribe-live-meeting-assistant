# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Publish-related data models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class StackPackageType(str, Enum):
    """How a stack is packaged during publish."""

    ZIP_AND_UPLOAD = "zip_and_upload"
    ZIP_WITH_TOKEN_REPLACE = "zip_with_token_replace"
    CFN_PACKAGE = "cfn_package"
    DELEGATE_SCRIPT = "delegate_script"
    BUILD_SCRIPT = "build_script"
    HASH_AND_PACKAGE = "hash_and_package"


class StackDefinition(BaseModel):
    """Definition of a stack for publishing."""

    name: str
    package_type: StackPackageType
    template_file: str = "template.yaml"
    source_dir: str = "."
    deployment_subdir: str | None = None
    delegate_script: str | None = None
    build_script: str | None = None
    hash_source_dir: str | None = None
    hash_template_field: str = "source_hash"
    s3_template_path: str | None = None
    supports_change_detection: bool = True


class PublishConfig(BaseModel):
    """Configuration for a publish operation."""

    bucket_basename: str
    prefix: str
    region: str
    public: bool = False
    project_dir: str = "."
    version: str = ""
    stacks: list[str] | None = None  # None = all stacks
    force: bool = False  # Skip change detection
    # When True, proceed even if there are untracked files inside BUILD_SCRIPT
    # stack directories. Default is False so publishes fail fast when new source
    # files haven't been `git add`-ed — those files would otherwise be silently
    # excluded from the build zip (the lma-ai-stack Makefile uses `git ls-files`
    # to assemble the source bundle).
    allow_untracked: bool = False


class StackPublishResult(BaseModel):
    """Result of publishing a single stack."""

    stack_name: str
    success: bool = True
    skipped: bool = False
    message: str = ""
    s3_template_url: str = ""
    duration_seconds: float = 0.0


class PublishResult(BaseModel):
    """Result of the full publish operation."""

    success: bool = True
    bucket: str = ""
    prefix: str = ""
    region: str = ""
    version: str = ""
    template_url: str = ""
    console_url: str = ""
    cli_deploy_command: str = ""
    stack_results: list[StackPublishResult] = Field(default_factory=list)
    duration_seconds: float = 0.0
    message: str = ""
