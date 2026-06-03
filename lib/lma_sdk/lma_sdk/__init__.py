# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""LMA SDK - Python SDK for AWS Live Meeting Assistant."""

__version__ = "0.3.4"

from lma_sdk.client import LMAClient
from lma_sdk.exceptions import (
    LMAAppSyncError,
    LMAAuthError,
    LMAConfigurationError,
    LMAError,
    LMAPublishError,
    LMAResourceNotFoundError,
    LMAStackError,
    LMATimeoutError,
    LMAValidationError,
    LMAVirtualParticipantError,
)

__all__ = [
    "LMAClient",
    "LMAError",
    "LMAConfigurationError",
    "LMAStackError",
    "LMAPublishError",
    "LMAResourceNotFoundError",
    "LMAValidationError",
    "LMAAuthError",
    "LMATimeoutError",
    "LMAAppSyncError",
    "LMAVirtualParticipantError",
]
