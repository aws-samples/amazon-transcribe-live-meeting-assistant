# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Consent gate for the tools that return captured meeting content.

The capture clients (the Virtual Participant, the WebSocket transcriber) stop
recording a meeting when its consent settings say capture is not allowed. A tool
that hands the captured content of a meeting to an MCP client applies the same
rule, with the same code: ``lma_consent.rules.capture_allowed``, from the
configuration layer, judged on the meeting's own ``Consent`` record and, when
that record names no mode, on the deployment's consent mode.

Reading the deployment's mode needs the configuration table. The MCP Lambda
already has ``dynamodb:GetItem`` on it (the search tool reads its model from the
same table) and ``kms:Decrypt`` on the customer-managed key that encrypts it, in
``MCPServerAnalyticsFunction`` of ``lma-ai-stack/deployment/lma-ai-stack.yaml``,
and it carries the configuration layer. No IAM change is needed.

Behaviour, in short:

* A meeting whose ``Consent`` record has a ``Mode`` is decided by that record
  alone. The deployment's settings are not read, so a settings failure cannot
  block a meeting whose own record decides.
* A meeting with no record, or one with no ``Mode``, is judged by the
  deployment's mode: only ``opt_in`` withholds it.
* Anything that stops the gate from deciding (the settings cannot be read, the
  layer is missing, the record cannot be understood) refuses the request with
  :class:`ConsentSettingsUnavailable`. The gate never allows content because it
  could not check. The cause is logged and not returned.
* A deployment without a configuration table name has the shipped defaults, in
  which consent is off.

The configuration layer is imported inside the functions, so the module loads
(and the server's other tools run) where the layer is absent.
"""

import logging
from collections.abc import Mapping
from typing import Any, Optional

logger = logging.getLogger()

#: Shown to the caller when the gate cannot decide. No cause, name or id in it.
SETTINGS_UNAVAILABLE_MESSAGE = "The meeting consent settings could not be read; try again"

#: Shown to the caller when the meeting's consent settings do not allow capture.
CAPTURE_NOT_ALLOWED_MESSAGE = (
    "Capture is not allowed for this meeting under its consent settings, "
    "so its content is not available"
)

#: The mode of a deployment with no consent settings: the shipped default.
DEFAULT_MODE = "off"

# One store per process: it only saves building the client on every call. The
# meeting_consent section is read with a strongly consistent read each time (no
# cache), so a change of the deployment's mode is seen by the next call.
_STORE: Optional[Any] = None


class ConsentSettingsUnavailable(PermissionError):
    """The gate could not decide whether capture is allowed; the caller may retry."""

    def __init__(self) -> None:
        super().__init__(SETTINGS_UNAVAILABLE_MESSAGE)


def deployment_consent_mode() -> str:
    """The deployment's consent mode: ``off``, ``notify`` or ``opt_in``.

    A deployment without the configuration table name has the shipped defaults,
    so the mode is ``off``. Every other failure raises
    :class:`ConsentSettingsUnavailable`: a deployment that requires consent must
    not release content because its settings could not be read.
    """
    global _STORE  # pylint: disable=global-statement
    try:
        # pylint: disable-next=import-error,import-outside-toplevel
        from lma_config import (
            MEETING_CONSENT,
            ConfigurationStore,
            ConfigurationTableNotConfigured,
            offered_settings,
        )

        # pylint: disable-next=import-error,import-outside-toplevel
        from lma_consent import rules
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("the configuration layer could not be loaded: %s", type(error).__name__)
        raise ConsentSettingsUnavailable() from None
    try:
        if _STORE is None:
            _STORE = ConfigurationStore()
        values = _STORE.get_section(MEETING_CONSENT.name).values
        settings = rules.effective_settings(offered_settings(MEETING_CONSENT.name, values))
        # effective_settings has already replaced a mode it does not know.
        return settings["mode"]
    except ConfigurationTableNotConfigured:
        logger.warning("CONFIGURATION_TABLE_NAME is unset; using the shipped consent defaults")
        return DEFAULT_MODE
    except Exception as error:  # pylint: disable=broad-exception-caught
        # The class name only: the message of a failed read names the table and the role.
        logger.error("could not read the meeting consent settings: %s", type(error).__name__)
        raise ConsentSettingsUnavailable() from None


def _known_mode(mode: Any, rules: Any) -> str:
    """``mode`` when it is one the rules know; anything else is not a decision."""
    if not isinstance(mode, str) or mode not in rules.MODES:
        raise ValueError("the deployment consent mode is not recognised")
    return mode


def capture_allowed(meeting: Any, deployment_mode: Optional[str] = None) -> bool:
    """Whether the meeting's captured content may be returned to a caller.

    Args:
        meeting: The meeting row (``c#<CallId>``), a mapping.
        deployment_mode: The deployment's consent mode, when the caller already
            has it. When it is None the mode is read, and only when the
            meeting's own ``Consent`` record has no ``Mode``: the rule ignores
            the deployment's mode otherwise.

    Raises:
        ConsentSettingsUnavailable: the gate could not decide.
    """
    if not isinstance(meeting, Mapping):
        logger.error("the meeting row is a %s, not a mapping", type(meeting).__name__)
        raise ConsentSettingsUnavailable()
    record = meeting.get("Consent")
    if record is not None and not isinstance(record, Mapping):
        logger.error("the meeting's Consent value is a %s, not a mapping", type(record).__name__)
        raise ConsentSettingsUnavailable()
    try:
        # pylint: disable-next=import-error,import-outside-toplevel
        from lma_consent import rules
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("the consent rules could not be loaded: %s", type(error).__name__)
        raise ConsentSettingsUnavailable() from None
    if record and record.get("Mode"):
        # The record decides; the rule does not look at the deployment's mode.
        mode = DEFAULT_MODE
    elif deployment_mode is not None:
        mode = deployment_mode
    else:
        mode = deployment_consent_mode()
    try:
        return bool(rules.capture_allowed(record, _known_mode(mode, rules)))
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("the meeting's consent record could not be judged: %s", type(error).__name__)
        raise ConsentSettingsUnavailable() from None


def require_capture_allowed(meeting: Any) -> None:
    """Return when capture is allowed for the meeting; raise otherwise.

    Raises:
        PermissionError: the meeting's consent settings do not allow capture.
        ConsentSettingsUnavailable: the gate could not decide (a PermissionError).
    """
    if not capture_allowed(meeting):
        raise PermissionError(CAPTURE_NOT_ALLOWED_MESSAGE)
