#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""How a configuration record is laid out inside one DynamoDB item.

The body -- everything an operator edits -- is serialised to JSON, gzipped, and
stored in the single Binary attribute ``Body``. A short, fixed list of
*metadata* attributes stays at the top level in plain form, so that the record's
provenance can be read, queried and displayed without decompressing anything.

Reads auto-detect the layout. An item with a ``Body`` attribute is decompressed;
an item without one is an *inline* record, and its remaining top-level
attributes are read as its body.

Nothing writes an inline record: the seeder and the resolvers both compress. The
detection is here so that one can be *read*, which matters in two cases. A later
store migration can lift an existing table's items into this one as they stand,
rather than needing a conversion pass before anything can read them; and a
record repaired by hand in the DynamoDB console is read as what it says rather
than as empty. The first ordinary write converts such a record, and until then
reads keep working.

Size is checked, not trimmed. DynamoDB's limit is 400 KB per item; a record
over 350 KB logs a warning, so an operator hears about it before the save that
fails, and one over the limit raises.
"""

from __future__ import annotations

import gzip
import json
import logging
from decimal import Decimal
from typing import Any, Dict, Mapping, Tuple

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from .errors import ConfigurationError, ConfigurationTooLarge

LOGGER = logging.getLogger(__name__)

#: The partition key attribute. One HASH key, no sort key.
KEY_ATTRIBUTE = "Configuration"

#: The compressed body.
BODY_ATTRIBUTE = "Body"

#: Attributes that stay readable at the top level of the item. A closed list:
#: anything else an operator sets is part of the body and is compressed with it.
METADATA_ATTRIBUTES: Tuple[str, ...] = (
    KEY_ATTRIBUTE,
    "CreatedAt",
    "UpdatedAt",
    # Written from the verified caller identity by the resolver that saves the
    # record -- the audit attributes added for the existing configuration
    # records, kept here so one reader serves both layouts.
    "UpdatedBy",
    "Version",
    "IsActive",
    "ActiveProfile",
    "Description",
    "Managed",
    # How a profile came to exist, written once when it is created and carried
    # forward by every later write. Durable on purpose: it is the only record of
    # whether an upgrade managed to carry an operator's prompts across, and the
    # seeder run that could report that is long past by the time anybody looks.
    "ProfileOrigin",
    "LatestRevision",
    "PublishedRevision",
)

MAX_ITEM_BYTES = 400 * 1024
WARN_ITEM_BYTES = 350 * 1024

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


def encode_body(body: Mapping[str, Any]) -> bytes:
    """Serialise and compress a configuration body.

    ``mtime=0`` keeps the gzip header free of a timestamp, so the same body
    always compresses to the same bytes. That is what keeps a rewrite of
    unchanged content from showing up as a changed item, and it is what lets a
    fixture of these bytes be committed and decoded by the TypeScript half's
    test suite -- which is the only direction worth comparing, since that half
    reads and never writes. Byte equality *between* the halves is not a thing to
    rely on: Node's gzip writes a different OS byte.
    """
    try:
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except TypeError as error:
        # Reported as itself rather than as a failed save: the caller's message
        # would otherwise say only that storing the configuration did not work.
        raise ConfigurationError(
            f"a configuration body must hold only JSON types: {error}"
        ) from error
    return gzip.compress(payload, compresslevel=9, mtime=0)


def decode_body(raw: Any) -> Dict[str, Any]:
    """Decompress a body read back from DynamoDB.

    Accepts the ``bytes`` the low-level client returns and the ``Binary``
    wrapper the resource API returns, since both reach this function depending
    on which client the caller built.
    """
    data = bytes(raw.value) if hasattr(raw, "value") else bytes(raw)
    decoded = json.loads(gzip.decompress(data).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("a configuration body must be a JSON object")
    return decoded


def _as_json_type(value: Any) -> Any:
    """Convert a deserialised DynamoDB value to something ``json.dumps`` accepts.

    ``TypeDeserializer`` returns a ``Decimal`` for every DynamoDB number and a
    ``set`` for every string or number set, and neither is a JSON type. A body
    holding one could be read but not written back, which would break the
    conversion the whole inline layout rests on: the first ordinary write of a
    record that arrived in that layout -- lifted in by a later store migration,
    or repaired by hand -- would fail on a numeric attribute.

    A number becomes an ``int`` when it is integral and a ``float`` otherwise,
    which is what the value would have been had it been written as JSON. A set
    becomes a sorted list, sorted rather than arbitrary so the body compresses
    to the same bytes every time.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(_as_json_type(item) for item in value)
    if isinstance(value, Mapping):
        return {key: _as_json_type(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_json_type(item) for item in value]
    return value


def unmarshal_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    """AttributeValue map -> plain Python, for items read with the low-level client.

    Every value is normalised to a JSON type, so a body this returns can be
    written straight back. The compressed ``Body`` needs no special case: it
    comes back as the ``Binary`` wrapper, which is not one of the types
    :func:`_as_json_type` converts and which :func:`decode_body` accepts.
    """
    return {name: _as_json_type(_DESERIALIZER.deserialize(value)) for name, value in item.items()}


def marshal_value(value: Any) -> Dict[str, Any]:
    """Plain Python -> AttributeValue, for the low-level client."""
    return _SERIALIZER.serialize(value)


def split_item(item: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Split a plain-Python item into ``(metadata, body)``.

    The layout is detected from the item rather than configured: a ``Body``
    attribute means the compressed layout, and its absence means an inline
    record whose remaining attributes are the body. Note the asymmetry -- when
    ``Body`` is present it is the whole body, and any other non-metadata
    attribute on the item is ignored rather than merged in, so a value added
    beside a compressed body has no effect.
    """
    metadata = {name: item[name] for name in METADATA_ATTRIBUTES if name in item}
    if BODY_ATTRIBUTE in item:
        return metadata, decode_body(item[BODY_ATTRIBUTE])
    body = {
        name: value
        for name, value in item.items()
        if name not in METADATA_ATTRIBUTES and name != BODY_ATTRIBUTE
    }
    return metadata, body


def check_item_size(key: str, compressed: bytes, metadata: Mapping[str, Any]) -> int:
    """Return the item's size in bytes, raising if it cannot be stored.

    Raises:
        ConfigurationTooLarge: the item exceeds DynamoDB's 400 KB limit.
    """
    size = len(compressed)
    for name, value in metadata.items():
        size += len(name.encode("utf-8"))
        size += len(str(value).encode("utf-8"))
    if size > MAX_ITEM_BYTES:
        raise ConfigurationTooLarge(
            f"the configuration record {key!r} compresses to {size} bytes, over "
            f"the {MAX_ITEM_BYTES}-byte DynamoDB item limit; split the "
            "configuration or shorten its longest values"
        )
    if size > WARN_ITEM_BYTES:
        LOGGER.warning(
            "the configuration record %r compresses to %d bytes, within %d "
            "bytes of the %d-byte DynamoDB item limit",
            key,
            size,
            MAX_ITEM_BYTES - size,
            MAX_ITEM_BYTES,
        )
    return size
