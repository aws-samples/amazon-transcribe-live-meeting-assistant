# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Whether, and how soon, a meeting's transcript is deleted once it is summarized.

The Retention section's ``transcriptDeletionAfterSummary`` setting, whose
default is the TranscriptDeletionAfterSummary stack parameter. Read by the
transcript deletion function, which acts on it, and by every writer of
transcript segments, which gives each segment a short expiry while deletion is
on so that DynamoDB's own expiry still removes it if the deletion never ran.
"""

import logging
from typing import TYPE_CHECKING, Optional

from .parameters import read_setting

if TYPE_CHECKING:
    from .store import ConfigurationStore

LOGGER = logging.getLogger(__name__)

SECTION = "retention"
SETTING = "transcriptDeletionAfterSummary"
ENV_VAR = "TRANSCRIPT_DELETION_AFTER_SUMMARY"

#: Each choice, as minutes after the summary is written; None is off.
DELAY_MINUTES = {
    "off": None,
    "immediately": 0,
    "after_15_minutes": 15,
    "after_30_minutes": 30,
    "after_60_minutes": 60,
}

#: How long the deletion waits for a summary that has not arrived, counted from
#: the end of the meeting, before the chosen delay starts counting anyway.
SUMMARY_WAIT_MINUTES = 60

#: The longest a transcript segment is given to live while deletion is on: the
#: expiry DynamoDB applies if the scheduled deletion never ran. Long enough
#: that a segment written at the start of a long meeting is still there when
#: the meeting is summarized.
SEGMENT_EXPIRY_LIMIT_DAYS = 2.0


def deletion_delay_minutes(*, store: Optional["ConfigurationStore"] = None) -> Optional[int]:
    """Minutes after the summary at which the transcript is deleted, or None when off.

    The section's value, else the stack parameter's environment variable, else
    off. A value that is not one of the choices is read as off and logged: the
    schema refuses to save one, and the parameter's AllowedValues refuse to
    deploy one.
    """
    choice = read_setting(SECTION, SETTING, env_var=ENV_VAR, default="off", store=store)
    if choice not in DELAY_MINUTES:
        LOGGER.warning(
            "%s › %s is %r, which is not a choice; reading it as off", SECTION, SETTING, choice
        )
        return None
    return DELAY_MINUTES[choice]
