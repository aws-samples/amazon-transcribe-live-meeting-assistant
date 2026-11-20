# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The email addresses an MCP client names to share a meeting with."""

import re
from typing import Any, List

# One recipient: an email address, as meeting_controls_resolver accepts it.
RECIPIENT_PATTERN = re.compile(r"[^@\[\]\s,]+@[^@\[\]\s,]+\.[^@\[\]\s,]+")
MAX_RECIPIENTS = 50


def recipients(value: Any, name: str = "users", allow_empty: bool = False) -> List[str]:
    """The addresses, trimmed and without repeats, in the order given.

    A comma-separated string is accepted as well as a list. Refused: anything
    that is not an email address, more than MAX_RECIPIENTS addresses, and, unless
    ``allow_empty``, none at all.
    """
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of email addresses")
    found: List[str] = []
    for item in value:
        address = item.strip()
        if not address:
            continue
        if not RECIPIENT_PATTERN.fullmatch(address):
            raise ValueError(f"{address!r} is not an email address")
        if address not in found:
            found.append(address)
    if not found and not allow_empty:
        raise ValueError(f"{name} must name at least one email address")
    if len(found) > MAX_RECIPIENTS:
        raise ValueError(f"{name} can name at most {MAX_RECIPIENTS} addresses at a time")
    return found
