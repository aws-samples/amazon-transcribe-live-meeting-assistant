#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The revision history of a configuration profile.

Every write that changes a profile's body records a *revision*: a numbered,
immutable copy of the body as it was saved, with who saved it, when, which
sections it changed and an optional comment. The store
(:mod:`lma_config.store`) decides when a revision is recorded; this module is
where revisions are kept and how they are read back.

Storage
-------
* **Bodies** are S3 objects at ``config_revisions/<profile>/<nnnnnn>.json.gz``,
  each a gzipped JSON *envelope* holding the body and the revision's metadata.
  The revision number is part of the key and is allocated exclusively (see
  below), so an object is written once and never replaced by a different
  revision; immutability comes from the key. The bucket is versioned as well,
  so even a deliberate overwrite or a prune leaves the previous object
  recoverable for the bucket's non-current retention window.
* **Metadata** lives in one DynamoDB item per profile, ``ConfigRevIndex#<name>``,
  holding a list of small entries. Listing a profile's history is one
  ``GetItem``. The prefix deliberately sits outside ``Config#`` so the index is
  never enumerated as a profile (see :mod:`lma_config.keys`).
* **Counters** live on the profile record: ``LatestRevision`` is the highest
  number ever allocated, ``PublishedRevision`` the revision the record's body
  *is*. One read of the profile says which revision is current.

Because the envelope carries the metadata too, the index is a convenience over
the objects rather than the only copy of anything: an entry that failed to be
appended can be rebuilt from its object.

Concurrency
-----------
Revision numbers are allocated in the same conditional ``PutItem`` that saves
the profile -- the number is one above both ``LatestRevision`` of the record the
write is conditioned on and the highest number the index lists, so only one
writer can ever commit a given number. (A separate atomic ``ADD`` would not be
safe here: the profile is written as a whole item, and a whole-item write would
reset a counter incremented beside it.) A profile whose history has not started
is given its first number by a conditional ``UpdateItem`` that sets only the
counters. A body is stored only under a number some committed write
allocated, so two racing writers cannot store different bodies under one
number. Appending
an entry uses DynamoDB's ``list_append``, which cannot lose a concurrent append;
the rarer read-modify-writes of the entry list -- pruning and labelling -- are
guarded by an ``IndexSeq`` counter and retried once.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from botocore.exceptions import ClientError

from . import codec
from .errors import ConfigurationError
from .keys import REVISION_INDEX_PREFIX, validate_profile_name

LOGGER = logging.getLogger(__name__)

#: The environment variable naming the bucket revision bodies are kept in. A
#: store built where it is unset keeps no history; see ``ConfigurationStore``.
REVISION_BUCKET_ENV_VAR = "CONFIGURATION_BUCKET_NAME"

#: The key prefix revision bodies are written under, inside that bucket.
REVISION_S3_PREFIX = "config_revisions"

#: How many revisions of a profile are kept. The published revision, anything
#: labelled and anything pinned are kept on top of these, however old.
DEFAULT_RETAINED_REVISIONS = 20

MAX_COMMENT_LENGTH = 500
MAX_LABEL_LENGTH = 100

#: How a revision came to be recorded.
SOURCE_SAVE = "save"  # an administrator's save from a configuration page or the API
SOURCE_RESTORE = "restore"  # an earlier revision saved again as a new one
SOURCE_DEPLOY = "deploy"  # written by the deploy-time seeder
#: The profile as it stood when history began: recorded from the profile itself,
#: so its author and time are those of whoever last saved it before then.
SOURCE_BACKFILL = "backfill"
#: A revision whose body was not stored when it was saved, recorded afterwards
#: from the profile record it still was. Its author and time are the record's;
#: which sections it changed, and its comment, are not known.
SOURCE_RECOVERED = "recovered"

_INDEX_ATTRIBUTE = "Revisions"
_SEQUENCE_ATTRIBUTE = "IndexSeq"
_CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
_DENIED_CODES = frozenset({"AccessDenied", "403", "Forbidden"})


def revision_index_key(name: str) -> str:
    """The partition key of a profile's revision index, after validating the name."""
    return f"{REVISION_INDEX_PREFIX}{validate_profile_name(name)}"


def revision_object_key(name: str, revision: int) -> str:
    """The S3 key a revision's body is stored under.

    The profile name goes into the key verbatim. That is safe only because
    :func:`validate_profile_name` restricts names to ``[A-Za-z0-9._-]`` and
    refuses anything starting ``__``; a name can hold no ``/`` and therefore
    cannot move the key out of its own prefix.
    """
    return f"{REVISION_S3_PREFIX}/{validate_profile_name(name)}/{int(revision):06d}.json.gz"


def clean_text(value: Any, limit: int) -> Optional[str]:
    """A trimmed, bounded comment or label, or ``None`` when there is none."""
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _canonical(value: Any) -> str:
    """One JSON rendering per value, for comparing two values for equality.

    Python's ``==`` says ``True == 1`` and ``1 == 1.0``, which would make a save
    that turned a switch from ``1`` into ``true`` look like no change at all.
    Comparing renderings keeps those apart.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def changed_sections(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[str]:
    """The top-level sections ``after`` adds, removes or changes relative to ``before``."""
    return sorted(
        name
        for name in set(before) | set(after)
        if name not in before
        or name not in after
        or _canonical(before[name]) != _canonical(after[name])
    )


def diff_bodies(
    before: Mapping[str, Any], after: Mapping[str, Any], path: Tuple[str, ...] = ()
) -> List[Dict[str, Any]]:
    """Every value that differs between two bodies, as a flat list of changes.

    Objects are compared key by key, recursively; anything else -- a list, a
    string, a number -- is compared as a whole value. Each change carries its
    ``Path`` as a list of keys rather than a joined string, because the keys
    themselves hold separators (``"1#SUMMARY"``), and ``Change`` is one of
    ``added``, ``removed`` or ``changed``.
    """
    changes: List[Dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        where = [*path, key]
        if key not in before:
            changes.append({"Path": where, "Change": "added", "After": after[key]})
        elif key not in after:
            changes.append({"Path": where, "Change": "removed", "Before": before[key]})
        else:
            old, new = before[key], after[key]
            if isinstance(old, Mapping) and isinstance(new, Mapping):
                changes.extend(diff_bodies(old, new, (*path, key)))
            elif _canonical(old) != _canonical(new):
                changes.append({"Path": where, "Change": "changed", "Before": old, "After": new})
    return changes


@dataclass(frozen=True)
class RevisionEntry:
    """One revision's metadata: what the index holds and the envelope repeats."""

    revision: int
    created_at: str
    created_by: str
    source: str
    #: The profile version the revision was saved as.
    version: int
    #: The sections this revision changed relative to the one before it. For
    #: the first revision of a profile, and for a backfilled one, every section
    #: it holds. ``None`` when that is not known (a recovered revision).
    sections: Optional[Tuple[str, ...]] = None
    comment: Optional[str] = None
    restored_from: Optional[int] = None
    label: Optional[str] = None
    #: Kept by the retention cap however old. Nothing in LMA pins a revision
    #: yet; the attribute is part of the retention rule so that stamping a
    #: meeting with the revision it ran under can rely on it.
    pinned: bool = False
    #: Uncompressed size of the body, in bytes.
    size_bytes: int = 0

    def to_plain(self) -> Dict[str, Any]:
        """The entry as the index item and the envelope store it."""
        plain: Dict[str, Any] = {
            "Revision": self.revision,
            "CreatedAt": self.created_at,
            "CreatedBy": self.created_by,
            "Source": self.source,
            "Version": self.version,
            "Pinned": self.pinned,
            "SizeBytes": self.size_bytes,
        }
        if self.sections is not None:
            plain["Sections"] = list(self.sections)
        for name, value in (
            ("Comment", self.comment),
            ("RestoredFrom", self.restored_from),
            ("Label", self.label),
        ):
            if value is not None:
                plain[name] = value
        return plain

    @classmethod
    def from_plain(cls, plain: Mapping[str, Any]) -> "RevisionEntry":
        sections = plain.get("Sections")
        restored = plain.get("RestoredFrom")
        return cls(
            revision=int(plain.get("Revision") or 0),
            created_at=str(plain.get("CreatedAt") or ""),
            created_by=str(plain.get("CreatedBy") or ""),
            source=str(plain.get("Source") or ""),
            version=int(plain.get("Version") or 0),
            sections=tuple(str(name) for name in sections) if sections is not None else None,
            comment=clean_text(plain.get("Comment"), MAX_COMMENT_LENGTH),
            restored_from=int(restored) if restored is not None else None,
            label=clean_text(plain.get("Label"), MAX_LABEL_LENGTH),
            pinned=bool(plain.get("Pinned", False)),
            size_bytes=int(plain.get("SizeBytes") or 0),
        )


@dataclass(frozen=True)
class RevisionIndex:
    """A profile's index item as read: its entries, newest first, and its sequence."""

    entries: Tuple[RevisionEntry, ...] = ()
    sequence: int = 0
    exists: bool = False

    def entry(self, revision: int) -> Optional[RevisionEntry]:
        for candidate in self.entries:
            if candidate.revision == revision:
                return candidate
        return None

    @property
    def highest_revision(self) -> int:
        """The highest number the index lists, or 0."""
        return max((entry.revision for entry in self.entries), default=0)


def _deduplicated(entries: Sequence[RevisionEntry]) -> Tuple[RevisionEntry, ...]:
    """One entry per revision number, newest first.

    Two entries can share a number in one way only: a revision whose body had
    not landed yet when a concurrent save recorded it from the profile record
    (``recovered``), and then its own save's entry arriving as well. Both
    describe the same body; the save's own entry is the more complete, so it is
    the one kept.
    """
    chosen: Dict[int, RevisionEntry] = {}
    for entry in entries:
        held = chosen.get(entry.revision)
        if held is None or (held.source == SOURCE_RECOVERED and entry.source != SOURCE_RECOVERED):
            chosen[entry.revision] = entry
    return tuple(sorted(chosen.values(), key=lambda entry: entry.revision, reverse=True))


@dataclass
class RevisionLog:
    """Reads and writes the revision bodies and the index for one table and bucket."""

    table_name: str
    bucket: str
    dynamodb: Any
    s3_factory: Callable[[], Any]
    now: Callable[[], str]
    retain: int = DEFAULT_RETAINED_REVISIONS
    _s3: Any = field(default=None, init=False, repr=False)

    @property
    def s3(self) -> Any:
        # Built on first use: most invocations of a resolver that carries this
        # never write a revision, and a client is not free to construct.
        if self._s3 is None:
            self._s3 = self.s3_factory()
        return self._s3

    # ── bodies ─────────────────────────────────────────────────────────────

    def put_body(self, name: str, entry: RevisionEntry, body: Mapping[str, Any]) -> None:
        """Write one revision's envelope."""
        envelope = {**entry.to_plain(), "Profile": name, "Body": dict(body)}
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=revision_object_key(name, entry.revision),
            Body=gzip.compress(payload, compresslevel=9, mtime=0),
            # Not ContentEncoding: gzip, which would invite an HTTP client to
            # decompress it on the way past and hand back something that is no
            # longer the stored bytes.
            ContentType="application/gzip",
        )

    def read_body(self, name: str, revision: int) -> Optional[Dict[str, Any]]:
        """One revision's envelope, or ``None`` when its object is not there.

        A ``403`` is not read as "not there". S3 answers a ``GetObject`` for a
        missing key with ``403`` rather than ``404`` when the caller lacks
        ``s3:ListBucket``, so the template grants that; with it in place, a
        ``403`` is a real permission problem, and reporting it as a missing
        revision would hide exactly the defect somebody needs to see.
        """
        key = revision_object_key(name, revision)
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            raw = response["Body"].read()
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in _MISSING_OBJECT_CODES:
                return None
            if code in _DENIED_CODES:
                raise ConfigurationError(
                    f"could not read the configuration revision s3://{self.bucket}/{key} "
                    f"({code}): the function needs s3:GetObject on {REVISION_S3_PREFIX}/* and "
                    "s3:ListBucket on the bucket, and kms:Decrypt on its key"
                ) from error
            raise
        envelope = json.loads(gzip.decompress(raw).decode("utf-8"))
        if not isinstance(envelope, dict) or not isinstance(envelope.get("Body"), dict):
            raise ConfigurationError(f"s3://{self.bucket}/{key} is not a configuration revision")
        return envelope

    def delete_body(self, name: str, revision: int) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=revision_object_key(name, revision))

    # ── the index ──────────────────────────────────────────────────────────

    def read_index(self, name: str) -> RevisionIndex:
        """The index item, read consistently: every caller is about to act on it."""
        response = self.dynamodb.get_item(
            TableName=self.table_name,
            Key={codec.KEY_ATTRIBUTE: {"S": revision_index_key(name)}},
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if not raw:
            return RevisionIndex()
        item = codec.unmarshal_item(raw)
        entries = [
            RevisionEntry.from_plain(plain)
            for plain in item.get(_INDEX_ATTRIBUTE) or []
            if isinstance(plain, Mapping)
        ]
        return RevisionIndex(
            entries=_deduplicated(entries),
            sequence=int(item.get(_SEQUENCE_ATTRIBUTE) or 0),
            exists=True,
        )

    def append(self, name: str, entries: Sequence[RevisionEntry]) -> None:
        """Add entries to the index with ``list_append``, which loses no concurrent append."""
        if not entries:
            return
        self.dynamodb.update_item(
            TableName=self.table_name,
            Key={codec.KEY_ATTRIBUTE: {"S": revision_index_key(name)}},
            UpdateExpression=(
                "SET #revisions = list_append(if_not_exists(#revisions, :empty), :new), "
                "#updated = :now ADD #seq :one"
            ),
            ExpressionAttributeNames={
                "#revisions": _INDEX_ATTRIBUTE,
                "#updated": "UpdatedAt",
                "#seq": _SEQUENCE_ATTRIBUTE,
            },
            ExpressionAttributeValues={
                ":empty": {"L": []},
                ":new": codec.marshal_value([entry.to_plain() for entry in entries]),
                ":now": {"S": self.now()},
                ":one": {"N": "1"},
            },
        )

    def rewrite(
        self,
        name: str,
        mutate: Callable[[Tuple[RevisionEntry, ...]], Optional[Sequence[RevisionEntry]]],
    ) -> bool:
        """Replace the entry list, guarded by ``IndexSeq`` and retried once.

        ``mutate`` receives the entries, newest first, and returns the list to
        store, or ``None`` to write nothing. Returns whether a list was stored.
        Every append also advances ``IndexSeq``, so an entry appended between
        the read and this write makes the write fail rather than vanish.
        """
        for attempt in (1, 2):
            index = self.read_index(name)
            updated = mutate(index.entries)
            if updated is None:
                return False
            condition = (
                "#seq = :seq" if index.exists and index.sequence else "attribute_not_exists(#seq)"
            )
            values: Dict[str, Any] = {
                ":entries": codec.marshal_value([entry.to_plain() for entry in updated]),
                ":now": {"S": self.now()},
                ":next": {"N": str(index.sequence + 1)},
            }
            if index.exists and index.sequence:
                values[":seq"] = {"N": str(index.sequence)}
            try:
                self.dynamodb.update_item(
                    TableName=self.table_name,
                    Key={codec.KEY_ATTRIBUTE: {"S": revision_index_key(name)}},
                    UpdateExpression="SET #revisions = :entries, #updated = :now, #seq = :next",
                    ConditionExpression=condition,
                    ExpressionAttributeNames={
                        "#revisions": _INDEX_ATTRIBUTE,
                        "#updated": "UpdatedAt",
                        "#seq": _SEQUENCE_ATTRIBUTE,
                    },
                    ExpressionAttributeValues=values,
                )
                return True
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != _CONDITIONAL_CHECK_FAILED:
                    raise
                LOGGER.info(
                    "the revision index of %r changed while it was being rewritten (attempt %d)",
                    name,
                    attempt,
                )
        LOGGER.warning("gave up rewriting the revision index of %r after a second conflict", name)
        return False

    # ── lifecycle ──────────────────────────────────────────────────────────

    def prune(self, name: str, *, protected: Sequence[int] = ()) -> List[int]:
        """Enforce the retention cap, returning the revisions removed.

        The newest ``retain`` revisions are kept, and so is anything labelled,
        pinned or named in ``protected`` -- the published revision, which is the
        profile's current state. The cap is a count, which an S3 lifecycle rule
        cannot express, so it is applied here. An entry leaves the index before
        its object is deleted, so a reader never finds an entry with no body
        behind it.
        """
        keep = {int(revision) for revision in protected}
        removed: List[int] = []

        def drop_beyond_the_cap(
            entries: Tuple[RevisionEntry, ...],
        ) -> Optional[List[RevisionEntry]]:
            removed.clear()
            survivors: List[RevisionEntry] = []
            for position, entry in enumerate(entries):
                if position < self.retain or entry.label or entry.pinned or entry.revision in keep:
                    survivors.append(entry)
                else:
                    removed.append(entry.revision)
            return survivors if removed else None

        if not self.rewrite(name, drop_beyond_the_cap):
            return []
        for revision in removed:
            try:
                self.delete_body(name, revision)
            except ClientError:
                # The entry is already gone, so nothing will read this object;
                # it is left for the bucket's own lifecycle rather than failing
                # a save that has already succeeded.
                LOGGER.exception("could not delete the pruned revision %d of %r", revision, name)
        if removed:
            LOGGER.info("pruned revisions %s of the configuration profile %r", removed, name)
        return list(removed)

    def set_label(self, name: str, revision: int, label: Optional[str]) -> bool:
        """Label a revision, or clear its label. A labelled revision is never pruned."""
        target = int(revision)

        def relabel(entries: Tuple[RevisionEntry, ...]) -> Optional[List[RevisionEntry]]:
            if not any(entry.revision == target for entry in entries):
                return None
            return [
                replace(entry, label=label) if entry.revision == target else entry
                for entry in entries
            ]

        return self.rewrite(name, relabel)
