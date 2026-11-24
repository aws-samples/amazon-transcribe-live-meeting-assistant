# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""An AWS call that failed, as one line for the caller.

The SDK's operations call AWS directly for the few things the LMA API does not
offer (a user's groups, the meetings table, the user pool). A failure there is
reported as an :class:`~lma_sdk.exceptions.LMAConfigurationError` naming what
could not be done; the IAM action the caller needs is named only when AWS
refused the call for want of a permission, so a throttle or a missing resource
does not send anyone to their IAM policy.
"""

from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError

from lma_sdk.exceptions import LMAConfigurationError

#: What an operation catches around an AWS call: botocore's own errors only, so
#: a defect in the SDK is not reported as an AWS failure.
AWS_ERRORS = (ClientError, BotoCoreError)


def error_code(err: Exception) -> str:
    """The AWS error code of a botocore ClientError, else ''."""
    response = getattr(err, "response", None) or {}
    return str((response.get("Error") or {}).get("Code") or "")


def is_access_denied(err: Exception) -> bool:
    """Whether AWS refused the call for want of a permission (``AccessDenied*``)."""
    return error_code(err).startswith("AccessDenied")


def aws_call_error(
    action: str, resource: str, err: Exception, see: str = ""
) -> LMAConfigurationError:
    """``Could not <resource>: <err>``, naming ``action`` when the call was denied.

    ``see`` is appended to the permission hint, for example a pointer to the
    documented permissions table.
    """
    if is_access_denied(err):
        hint = f" ({see})" if see else ""
        return LMAConfigurationError(
            f"Could not {resource}; the caller needs {action} on it{hint}: {err}"
        )
    return LMAConfigurationError(f"Could not {resource}: {err}")
