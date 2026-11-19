# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Size budget for the text of one answer.

The server writes an answer as JSON with every non-ASCII character as a ``\\uXXXX``
escape, and the API Gateway body escapes that again. Counting characters would
let a text of Chinese characters or emoji come to several times the size of an
ASCII one, so the budget counts units: an ASCII character is 1, any other
character in the first plane 3, and a character beyond it 6. A control
character other than a line break or a tab, and DEL, are 2: each is written as a
six-character escape (seven bytes in the body); texts and titles have them
turned into spaces first, but an id is shown as stored.

Worst case, the characters that are escaped twice (a quote, a backslash) cost 4
bytes for 1 unit, and no character costs more than 4 bytes a unit, so an answer
whose texts add up to 20,000 units stays under 100 KB with its fields around
them.
"""

from typing import List, Optional, Sequence, Tuple

# The text of one answer is held to this many units, however many items it has.
TEXT_BUDGET = 20_000


def char_units(char: str) -> int:
    """What one character costs in the answer's text budget."""
    code = ord(char)
    if (code < 0x20 and char not in "\n\t") or code == 0x7F:
        return 2
    return 1 if code < 0x80 else 3 if code <= 0xFFFF else 6


def units_of(text: str) -> int:
    """What a text costs in the answer's text budget."""
    return sum(char_units(c) for c in text)


def cut_to_units(text: str, allowed: int) -> str:
    """The longest start of ``text`` that costs at most ``allowed`` units."""
    used = 0
    for position, char in enumerate(text):
        used += char_units(char)
        if used > allowed:
            return text[:position]
    return text


def plain_text(text: str) -> str:
    """The text with control characters (but not line breaks or tabs) turned into spaces.

    Each is written as a six-character escape, so a text of them would take
    the most room for the least to read.
    """
    return "".join(
        " " if (ord(c) < 0x20 and c not in "\n\t") or ord(c) == 0x7F else c for c in text
    )


def fit_texts(
    texts: Sequence[Optional[str]], budget: int = TEXT_BUDGET
) -> List[Tuple[Optional[str], bool]]:
    """Each text cut to its share of ``budget`` units, with whether it was cut.

    Shares are worked out from the shortest text up: each text gets at most an
    equal share of what is left, so a short text keeps all of its room and gives
    what it does not use to the longer ones. A text that is None stays None.
    """
    cut = []
    for text in texts:
        if text is None:
            cut.append((None, False))
            continue
        text = plain_text(text)
        # Nothing longer than the whole budget can be kept, so nothing longer is measured.
        head = cut_to_units(text, budget)
        cut.append((head, len(head) < len(text)))

    order = sorted(
        (i for i, (head, _) in enumerate(cut) if head is not None),
        key=lambda i: units_of(cut[i][0]),
    )
    remaining = budget
    fitted: List[Tuple[Optional[str], bool]] = list(cut)
    for turn, i in enumerate(order):
        head, was_cut = cut[i]
        share = remaining // (len(order) - turn)
        kept = cut_to_units(head, share)
        remaining -= units_of(kept)
        fitted[i] = (kept, was_cut or len(kept) < len(head))
    return fitted
