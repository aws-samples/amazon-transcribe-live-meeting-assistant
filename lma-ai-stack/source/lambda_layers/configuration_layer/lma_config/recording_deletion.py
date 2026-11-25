# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Whether, and how soon, a meeting's recordings are deleted once it is summarized.

The Retention section's ``recordingDeletionAfterSummary`` setting, independent
of ``transcriptDeletionAfterSummary`` (transcript_deletion.py) and with the
same choices. It has no stack parameter: the configuration seeder ships it set
to ``off`` in the deployment's baseline
(``configuration_seeder/defaults/Retention.json``), and an Admin changes it on
the Configuration page. Read by the transcript deletion function, which also
deletes the recordings (docs/transcript-deletion.md).
"""

import logging
from typing import TYPE_CHECKING, Optional

from .parameters import _shared_store
from .transcript_deletion import DELAY_MINUTES, SECTION

if TYPE_CHECKING:
    from .store import ConfigurationStore

LOGGER = logging.getLogger(__name__)

SETTING = "recordingDeletionAfterSummary"


def recording_deletion_delay_minutes(
    *, store: Optional["ConfigurationStore"] = None
) -> Optional[int]:
    """Minutes after the summary at which the recordings are deleted, or None when off.

    Read as deletion_delay_minutes reads the transcript's setting: an Admin's
    choice, else the shipped ``off``; off with no configuration table named;
    a value that is not one of the choices is read as off and logged.

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
