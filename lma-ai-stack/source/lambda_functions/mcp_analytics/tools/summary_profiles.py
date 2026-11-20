# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: list_summary_profiles, and the summaryProfile argument the meeting tools take.

A meeting's summary profile names the set of summary sections it is summarised
with: ``default`` (the Transcript Summary prompts) or an entry of the
``summary_profiles`` configuration section. The list is read from the shared
configuration table, as the meeting assistant's model settings are. Where a
profile is stored -- on a Virtual Participant, on a meeting -- the meeting
preparation resolver checks it against the configuration again, so the check
here only gives the client a plain answer before the write. See
docs/transcript-summarization.md#summary-profiles.
"""

from typing import Any, Dict, List, Optional

# pylint: disable=import-error
from lma_summary_profiles import (  # configuration layer
    DEFAULT_PROFILE,
    UnknownSummaryProfile,
    normalize_profile_name,
    profile_listing,
    require_profile,
)

# pylint: enable=import-error

_STORE: Any = None


def _section() -> Dict[str, Any]:
    """The configuration's summary_profiles section, through its declared cache tier."""
    global _STORE  # pylint: disable=global-statement
    # pylint: disable-next=import-error,import-outside-toplevel
    from lma_config import SUMMARY_PROFILES, ConfigurationStore

    if _STORE is None:
        _STORE = ConfigurationStore()
    return dict(_STORE.get_section(SUMMARY_PROFILES.name).values)


def profile_argument(value: Any) -> Optional[str]:
    """A summaryProfile argument's form checked: the name, "" for the default, or None if absent."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("summaryProfile must be a summary profile name")
    try:
        return normalize_profile_name(value) or ""
    except ValueError as error:
        raise ValueError(f"summaryProfile: {error}") from error


def existing_profile(value: Any) -> Optional[str]:
    """A summaryProfile argument checked against the configuration: None for the default."""
    try:
        return require_profile(value, _section())
    except UnknownSummaryProfile as error:
        raise ValueError(str(error)) from error
    except ValueError as error:
        raise ValueError(f"summaryProfile: {error}") from error


def list_profiles() -> Dict[str, Any]:
    """list_summary_profiles: every profile a meeting may name, default first."""
    profiles: List[Dict[str, Any]] = profile_listing(_section())
    return {"profiles": profiles, "default": DEFAULT_PROFILE}
