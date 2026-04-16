# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""LMA SDK exceptions hierarchy."""


class LMAError(Exception):
    """Base exception for all LMA SDK errors."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class LMAConfigurationError(LMAError):
    """Raised when SDK configuration is invalid or incomplete."""


class LMAStackError(LMAError):
    """Raised for CloudFormation stack operation failures."""


class LMAPublishError(LMAError):
    """Raised when artifact publishing fails."""


class LMAResourceNotFoundError(LMAError):
    """Raised when a required AWS resource cannot be found."""


class LMAValidationError(LMAError):
    """Raised when input validation fails."""


class LMAAuthError(LMAError):
    """Raised when authentication fails."""


class LMATimeoutError(LMAError):
    """Raised when an operation times out."""
