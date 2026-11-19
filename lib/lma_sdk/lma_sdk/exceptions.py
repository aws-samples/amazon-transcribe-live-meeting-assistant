# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
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


class LMAAppSyncError(LMAError):
    """Raised when an AppSync GraphQL call returns errors."""

    @property
    def error_type(self) -> str | None:
        """The first GraphQL error's ``errorType``: the refusal a resolver
        names (``MeetingNotFound``, for example), or None."""
        errors = self.details.get("errors") or []
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
        return first.get("errorType")


class LMAVirtualParticipantError(LMAError):
    """Raised when a Virtual Participant operation fails."""
