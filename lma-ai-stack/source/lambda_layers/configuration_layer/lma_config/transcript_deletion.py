# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Whether, and how soon, a meeting's transcript is deleted once it is summarized.

The Retention section's ``transcriptDeletionAfterSummary`` setting. It has no
stack parameter: the configuration seeder ships it set to ``off`` in the
deployment's baseline (``configuration_seeder/defaults/Retention.json``), and
an Admin changes it on the Configuration page. Read by the transcript deletion
function, which acts on it, and by every writer of transcript segments, which
gives each segment a short expiry while deletion is on so that DynamoDB's own
expiry still removes it if the deletion never ran.
"""

import logging
from typing import TYPE_CHECKING, Optional

from .parameters import _shared_store

if TYPE_CHECKING:
    from .store import ConfigurationStore

LOGGER = logging.getLogger(__name__)

SECTION = "retention"
SETTING = "transcriptDeletionAfterSummary"

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

    The section's resolved value: an Admin's choice, else the shipped ``off``.
    With no configuration table named, off. A value that is not one of the
    choices is read as off and logged; the schema refuses to save one.

    Raises:
        The store's errors when the section cannot be read, so that a caller
        deciding whether to delete does not take an unreadable setting for off.
    """
    reader = store if store is not None else _shared_store()
    if reader is None:
        return None
    choice = reader.get_section(SECTION).values.get(SETTING, "off")
    if choice not in DELAY_MINUTES:
        LOGGER.warning(
            "%s › %s is %r, which is not a choice; reading it as off", SECTION, SETTING, choice
        )
        return None
    return DELAY_MINUTES[choice]
