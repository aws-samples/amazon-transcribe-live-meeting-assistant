# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The web addresses in a meeting chat message, found by rule rather than by a model.

A meeting summary lists the links shared in the chat. Those addresses come from
here, out of the stored messages, and never from the model that writes the
summary: a model asked to copy a link can change it, and a chat message can ask
it to. ``extract_links`` finds them, and ``strip_links`` takes every address out
of a model's text so that the only links a summary carries are the ones listed
from this module.

The rules, which the UI's twin (``ui/src/components/common/chat-links.js``)
applies too, and which ``test-fixtures/chat-links.json`` at the repository root
holds both to:

* A candidate starts with ``http://``, ``https://`` or ``www.`` (any case), not
  directly after a letter, digit or one of ``@ . / : -``, and runs to the next
  whitespace, ``<``, ``>`` or ``"``.
* Trailing punctuation is trimmed (``. , ; : ! ? ' " * _ ~`` and the ellipsis),
  and so is a trailing ``)``, ``]`` or ``}`` that has no opening partner in the
  address, repeatedly, so ``(see https://example.com/a_(b)).`` gives
  ``https://example.com/a_(b)``.
* A ``www.`` address is given ``https://``, and the scheme is written in lower
  case; the rest is kept as typed.
* Only an ``http`` or ``https`` address with a host is kept, and one longer than
  2,048 characters is dropped.
* A backtick is written ``%60``, so an address can sit in a Markdown code span.
* Each address is listed once, where it first appears.

Pure Python with no dependencies, like the rest of this layer.
"""

from __future__ import annotations

import re
from typing import Iterable, List

#: Longest address kept, in characters, after the changes above.
MAX_LINK_CHARS = 2048

# A candidate address. The lookbehind keeps "user@www.example.com" and the
# middle of another word from starting one.
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9@./:\-])(?:https?://|www\.)[^\s<>\"]+", re.IGNORECASE)

_TRAILING_PUNCTUATION = set(".,;:!?'\"*_~…")
_CLOSERS = {")": "(", "]": "[", "}": "{"}

# An http or https address with a host: optional user part, then at least one
# host character, an optional port, and then the end or a path, query or fragment.
_VALID = re.compile(r"^https?://(?:[^/?#@\s]*@)?[^/?#@:\s]+(?::\d*)?(?:[/?#]|$)", re.IGNORECASE)


def _trim(candidate: str) -> str:
    url = candidate
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCTUATION:
            url = url[:-1]
            continue
        opener = _CLOSERS.get(last)
        if opener is not None and url.count(last) > url.count(opener):
            url = url[:-1]
            continue
        break
    return url


def _normalised(candidate: str) -> str:
    url = _trim(candidate)
    if url[:4].lower() == "www.":
        url = "https://" + url
    scheme, sep, rest = url.partition("://")
    url = scheme.lower() + sep + rest
    return url.replace("`", "%60")


def extract_links(text: object) -> List[str]:
    """Every web address in ``text``, in order of first appearance, each once."""
    if not isinstance(text, str) or not text:
        return []
    found: List[str] = []
    seen = set()
    for match in _CANDIDATE.finditer(text):
        url = _normalised(match.group(0))
        if not _VALID.match(url) or len(url) > MAX_LINK_CHARS or url in seen:
            continue
        seen.add(url)
        found.append(url)
    return found


def extract_links_from(texts: Iterable[object]) -> List[str]:
    """``extract_links`` over several texts, each address once across all of them."""
    found: List[str] = []
    seen = set()
    for text in texts:
        for url in extract_links(text):
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


# ── taking addresses out of a model's text ─────────────────────────────────
#
# Broader than extract_links on purpose: what is removed here is anything a
# Markdown renderer could turn into a link or a picture, or that a reader could
# copy into a browser, whatever its scheme.

_MARKDOWN_TARGET = r"\((?:[^()\n]|\([^()\n]*\))*\)"
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]\n]*)\]" + _MARKDOWN_TARGET)
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]*)\]" + _MARKDOWN_TARGET)
_REFERENCE_LINK = re.compile(r"\[([^\]\n]*)\]\[[^\]\n]*\]")
# Only a definition whose target is an address: "[00:30]: Introduction" in a
# section's text is a time, not a link definition.
_REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*<?"
    r"(?:[A-Za-z][A-Za-z0-9+.\-]*://|www\.|(?:javascript|data|vbscript|mailto|file):)\S*.*$",
    re.MULTILINE | re.IGNORECASE,
)
_HTML_TAG = re.compile(r"</?[A-Za-z!][^<>]*>")
_ANY_SCHEME_ADDRESS = re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s<>\"]*")
_SCHEMELESS_ADDRESS = re.compile(
    r"(?<![A-Za-z0-9@./\-])(?:www\.[^\s<>\"]+|(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}/[^\s<>\"]*)",
    re.IGNORECASE,
)
_INLINE_SCHEMES = re.compile(
    r"\b(?:javascript|data|vbscript|mailto|file):[^\s<>\"]+", re.IGNORECASE
)

#: What a removed address is replaced with.
LINK_REMOVED = "(link)"


def _removed(match: "re.Match[str]") -> str:
    # The punctuation after an address, and a closing bracket it does not
    # open, belong to the sentence around it, so they stay.
    address = match.group(0)
    return LINK_REMOVED + address[len(_trim(address)) :]


def strip_links(text: object) -> str:
    """``text`` with every link, picture and web address taken out.

    A Markdown link or picture keeps its text; an address written out, whatever
    its scheme, becomes ``(link)``; an HTML tag and a reference-style link
    definition are removed.
    """
    if not isinstance(text, str):
        return ""
    out = _MARKDOWN_IMAGE.sub(lambda m: m.group(1), text)
    out = _MARKDOWN_LINK.sub(lambda m: m.group(1), out)
    out = _REFERENCE_LINK.sub(lambda m: m.group(1), out)
    out = _REFERENCE_DEFINITION.sub("", out)
    out = _HTML_TAG.sub("", out)
    out = _ANY_SCHEME_ADDRESS.sub(_removed, out)
    out = _SCHEMELESS_ADDRESS.sub(_removed, out)
    out = _INLINE_SCHEMES.sub(LINK_REMOVED, out)
    return out


__all__ = [
    "LINK_REMOVED",
    "MAX_LINK_CHARS",
    "extract_links",
    "extract_links_from",
    "strip_links",
]
