#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for how a configuration record is laid out inside a DynamoDB item.

Three properties are asserted on values rather than on shapes, because each one
is silent when wrong:

* **Round-tripping.** A body that comes back altered is a configuration that
  quietly stops matching what the operator saved.
* **Layout auto-detection.** Nothing writes an *inline* record -- one with its
  values at the top level and no compressed body -- but one can be read: a
  later store migration can lift an existing table's items in as they stand,
  and a record repaired by hand in the console has that shape. If reads did
  not recognise it, such a record would read as empty, which for prompt
  templates means summaries generated with no prompts at all.
* **The size guard.** DynamoDB rejects an item over 400 KB. The record either
  raises here, where the message says what to do, or fails inside DynamoDB with
  a message about an item -- and a guard that trimmed instead would store a
  configuration missing its tail.
"""

from __future__ import annotations

import base64
import gzip
import json

import pytest

from lma_config import codec
from lma_config.errors import ConfigurationTooLarge

BODY = {
    "summary_prompt_templates": {
        "1#SUMMARY": "Summarise <transcript>{transcript}</transcript>",
        "2#ACTIONS": "List the actions",
    }
}


def test_a_body_round_trips_through_the_compressed_attribute() -> None:
    assert codec.decode_body(codec.encode_body(BODY)) == BODY


def test_the_same_body_always_compresses_to_the_same_bytes() -> None:
    """An unchanged record has to be recognisable as unchanged.

    gzip writes a modification time into its header by default, which would
    make two encodings of one body differ and defeat any comparison of stored
    bytes.
    """
    assert codec.encode_body(BODY) == codec.encode_body(dict(BODY))
    # Key order in the source mapping must not show up in the bytes either.
    section = BODY["summary_prompt_templates"]
    reordered = {"summary_prompt_templates": dict(reversed(list(section.items())))}
    assert codec.encode_body(reordered) == codec.encode_body(BODY)


def test_the_body_is_stored_as_gzipped_json() -> None:
    """Pinned so the TypeScript reader can be written against the same bytes."""
    raw = codec.encode_body(BODY)
    assert raw[:2] == b"\x1f\x8b", "not a gzip stream"
    assert json.loads(gzip.decompress(raw).decode("utf-8")) == BODY


# The body and the bytes the TypeScript half's suite commits as a fixture, so
# neither side can change the encoding alone. That half reads and never writes,
# so bytes produced here and decoded there is the only direction worth checking
# -- and a round-trip in either language cannot establish it.
ENCODED_FIXTURE_BODY = {
    "summary_prompt_templates": {
        "1#SUMMARY": "Summarise <transcript>{transcript}</transcript> using ’ and 20°.",
        "2#ACTIONS": "List the actions",
    },
    "numbers": {"whole": 512, "fraction": 0.25},
}
ENCODED_FIXTURE_BASE64 = (
    "H4sIAAAAAAAC/02O0QqCMBiFX2X83orNgRcNCaSrIAuyLgJBpq0cuCnbPyJk717YRd0dON/5ODMYr1tpHfAZ7lZ0qEYDnCYs"
    "i+HZj4MEnqUsxOC81sK+msmOesIGpZ4GgXIZplF1KcvidAUO1cIpJ0mOVhjXWTXhZv7lkK/+CuKdMg9Se0bTNRHmRhitPaUt"
    "TSAGFhXb8+54qD7ivXJIsJfke9JBCG8kvhxcvgAAAA=="
)


def test_the_committed_fixture_is_what_this_encoder_produces() -> None:
    """The TypeScript suite decodes exactly these bytes.

    If this fails, the encoder changed: regenerate the base64 and update the
    copy in `configuration-store.test.ts`, which has the command in a comment.
    Changing one side alone is the failure this pair exists to prevent.
    """
    assert base64.b64encode(codec.encode_body(ENCODED_FIXTURE_BODY)).decode() == (
        ENCODED_FIXTURE_BASE64
    )
    assert codec.decode_body(base64.b64decode(ENCODED_FIXTURE_BASE64)) == ENCODED_FIXTURE_BODY


def test_a_compressed_item_is_split_into_metadata_and_body() -> None:
    item = {
        "Configuration": "Config#custom",
        "CreatedAt": "2026-01-01T00:00:00Z",
        "UpdatedAt": "2026-02-02T00:00:00Z",
        "UpdatedBy": "admin@example.com",
        "Version": 3,
        "Body": codec.encode_body(BODY),
    }
    metadata, body = codec.split_item(item)
    assert body == BODY
    assert metadata == {
        "Configuration": "Config#custom",
        "CreatedAt": "2026-01-01T00:00:00Z",
        "UpdatedAt": "2026-02-02T00:00:00Z",
        "UpdatedBy": "admin@example.com",
        "Version": 3,
    }


def test_an_inline_item_is_read_as_its_own_body() -> None:
    """An item with no compressed body has its values at the top level.

    Nothing here writes one; this is what lets a record be read that arrived
    some other way -- lifted in by a later store migration, or repaired by hand
    in the console. The metadata attributes are still recognised as metadata, so
    the record's provenance does not end up inside its body.
    """
    item = {
        "Configuration": "Config#custom",
        "UpdatedBy": "admin@example.com",
        "1#SUMMARY": "a prompt",
        "*Information*": "guidance text",
    }
    metadata, body = codec.split_item(item)
    assert metadata == {"Configuration": "Config#custom", "UpdatedBy": "admin@example.com"}
    assert body == {"1#SUMMARY": "a prompt", "*Information*": "guidance text"}


def test_an_item_with_neither_a_body_nor_other_attributes_reads_as_empty() -> None:
    """The active-profile pointer is metadata only, and must not read as a body."""
    metadata, body = codec.split_item(
        {"Configuration": "Config#__active", "ActiveProfile": "custom"}
    )
    assert metadata == {"Configuration": "Config#__active", "ActiveProfile": "custom"}
    assert body == {}


def test_the_metadata_list_is_closed() -> None:
    """Anything not on the list is part of the body and is compressed with it.

    Asserted as an exact tuple because the list is the contract the TypeScript
    reader and any future AppSync resolver are written against: an attribute
    added here becomes readable without decompressing, and one removed stops
    being.
    """
    assert codec.METADATA_ATTRIBUTES == (
        "Configuration",
        "CreatedAt",
        "UpdatedAt",
        "UpdatedBy",
        "Version",
        "IsActive",
        "ActiveProfile",
        "Description",
        "Managed",
        "ProfileOrigin",
        "LatestRevision",
        "PublishedRevision",
    )


def test_a_record_that_fits_is_accepted() -> None:
    body = {"section": {"key": "x" * 1000}}
    size = codec.check_item_size("Config#custom", codec.encode_body(body), {})
    assert 0 < size < codec.WARN_ITEM_BYTES


def test_a_record_that_would_not_fit_raises_rather_than_being_trimmed() -> None:
    """Random bytes, because text this size compresses below the limit."""
    incompressible = {"section": {"key": _incompressible(600 * 1024)}}
    with pytest.raises(ConfigurationTooLarge) as raised:
        codec.check_item_size(
            "Config#custom", codec.encode_body(incompressible), {"Configuration": "Config#custom"}
        )
    assert "400" in str(raised.value) or "409600" in str(raised.value)


def test_a_record_close_to_the_limit_is_stored_and_warned_about(caplog) -> None:
    """The warning is the operator's notice before the save that fails."""
    compressed = b"x" * (codec.WARN_ITEM_BYTES + 1)
    with caplog.at_level("WARNING", logger="lma_config.codec"):
        size = codec.check_item_size("Config#custom", compressed, {})
    assert size == len(compressed)
    assert "item limit" in caplog.text


def test_the_limit_matches_dynamodbs_own() -> None:
    assert codec.MAX_ITEM_BYTES == 400 * 1024
    assert codec.WARN_ITEM_BYTES == 350 * 1024


def _incompressible(length: int) -> str:
    """A string gzip cannot shrink, built without a random source."""
    alphabet = "".join(chr(code) for code in range(33, 127))
    digits = []
    value = 1
    while len(digits) < length:
        value = (value * 1103515245 + 12345) % (2**31)
        digits.append(alphabet[value % len(alphabet)])
    return "".join(digits)
