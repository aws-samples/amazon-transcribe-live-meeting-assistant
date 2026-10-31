#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The shared accessor for the configuration table.

One class, used by every Lambda that reads or writes runtime configuration, so
that the key layout, the compressed body, the caching policy and the
concurrency guard exist once rather than once per store.

Reading
-------
:meth:`ConfigurationStore.get_section` resolves the document -- the baseline
``Config#default`` with the active profile's delta merged over it -- and returns
one section of it. How long a previously resolved document may be reused comes
from the section's declared tier (see :mod:`lma_config.sections`); there is no
TTL argument.

Writing
-------
Every write that an operator can trigger is a conditional one. The caller passes
the version it read, and a save is refused if the record moved on in between, so
two admin sessions saving the same page cannot silently discard one another's
edits. ``Config#default`` is the exception and is written unconditionally: it is
the deployment baseline and CloudFormation is its only author.

History
-------
When the store is given a revision bucket, every write that changes a profile's
body also records a revision (see :mod:`lma_config.revisions`). The rule that
holds it together: **a profile's body is never replaced until the revision it
is has been recorded.** So a save first makes sure of the record it is about to
replace -- recording it if history has not started yet, or if its own save
could not store it -- and refuses to proceed if that fails; then it writes the
profile, allocating the new revision's number in that same conditional write;
and then it stores the new revision. If that last step fails the save stands,
and the next save records it from the profile before replacing it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import codec
from .errors import (
    ConcurrentModification,
    ConfigurationError,
    ConfigurationTableNotConfigured,
    RevisionHistoryUnavailable,
    RevisionNotFound,
)
from .keys import (
    ACTIVE_POINTER_KEY,
    CONFIG_PREFIX,
    DEFAULT_KEY,
    DEFAULT_PROFILE_NAME,
    is_profile_key,
    profile_key,
    validate_profile_name,
)
from .revisions import (
    DEFAULT_RETAINED_REVISIONS,
    MAX_COMMENT_LENGTH,
    MAX_LABEL_LENGTH,
    REVISION_BUCKET_ENV_VAR,
    SOURCE_BACKFILL,
    SOURCE_DEPLOY,
    SOURCE_RECOVERED,
    SOURCE_RESTORE,
    SOURCE_SAVE,
    RevisionEntry,
    RevisionLog,
    changed_sections,
    clean_text,
    diff_bodies,
)
from .sections import (
    TIER_CONSISTENT_READ,
    TIER_TTL_SECONDS,
    CacheTier,
    SectionSpec,
    section_spec,
)

LOGGER = logging.getLogger(__name__)

#: The one environment variable naming the table. Spelled the same way in every
#: template and every handler: two names for one table is a small thing that
#: becomes confusing quickly.
TABLE_NAME_ENV_VAR = "CONFIGURATION_TABLE_NAME"

_CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"

#: The author recorded for what the deploy-time seeder writes.
_SEEDER = "cloudformation"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _s3_client() -> Any:
    return boto3.client("s3")


def _size_of(body: Mapping[str, Any]) -> int:
    """A body's uncompressed size in bytes, as its revision records it."""
    return len(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _digestible(value: Any) -> Any:
    """Normalise a value so both halves of the accessor serialise it alike.

    Two differences between the serialisers are taken out here, because both
    can arise from a configuration body somebody would plausibly write:

    * A float that happens to be integral. Python writes ``1.0``; JavaScript
      writes ``1``. Converted to an ``int``, which is what both then write.
    * Non-ASCII text, handled by ``ensure_ascii=False`` in the caller.

    They are not the only two differences, and the rest are deliberately left:
    Python writes ``1e-05`` where JavaScript writes ``0.00001``; an integral
    float of 1e21 or more becomes an ``int`` that prints in full where
    JavaScript prints ``1e+21``; and sorting keys compares code points here and
    UTF-16 code units there, which differ only above the basic multilingual
    plane. A configuration holding a value like that would give the two halves
    different digests.

    That is a bounded cost, which is why the line is drawn here rather than at a
    full canonical-number implementation. A fingerprint is a hint, not a
    decision: the values a reader uses come from the document itself, and a
    mismatch makes a reader rebuild derived state it did not need to rebuild --
    or skip a rebuild it did -- rather than making it behave differently. If a
    section ever needs to hold a number of that kind, canonicalise it before it
    is stored, and extend the vector both suites pin.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {key: _digestible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_digestible(item) for item in value]
    return value


def _fingerprint(values: Any) -> str:
    """A short, stable digest of a resolved value.

    Lets a caller tell a refetch that changed nothing from one that did, so work
    derived from the configuration -- a connected client, a compiled template --
    is rebuilt only when it has to be. The same idea as the MCP server loader's
    config fingerprint, which is the policy the standard tier adopts.

    The digest has to match the TypeScript half's for the same section, or a
    caller comparing one taken by each would rebuild its derived state forever,
    or never. :func:`_digestible` and ``ensure_ascii=False`` are what make the
    two serialisations identical; ``test_configuration_store_contract.py`` pins
    one digest on each side against the same input, which is what keeps it
    honest.
    """
    canonical = json.dumps(
        _digestible(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """``overlay`` over ``base``, recursing into nested objects.

    A value in the overlay replaces the one in the base, except where both are
    objects, in which case they are merged key by key. That is what makes a
    profile a *delta*: it holds only the keys an operator changed, and the rest
    keep coming from the baseline the deployment shipped.
    """
    merged: Dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class ConfigurationRecord:
    """One stored record: its body, its metadata and its version."""

    key: str
    body: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)
    exists: bool = True

    @property
    def version(self) -> int:
        """The record's version, or 0 when it has never been written."""
        raw = self.metadata.get("Version")
        return int(raw) if raw is not None else 0

    @property
    def profile_origin(self) -> str:
        """How this profile came to exist, or "" for a record without the mark."""
        return str(self.metadata.get("ProfileOrigin", ""))

    @property
    def updated_by(self) -> str:
        return str(self.metadata.get("UpdatedBy", ""))

    @property
    def updated_at(self) -> str:
        return str(self.metadata.get("UpdatedAt", ""))

    @property
    def latest_revision(self) -> int:
        """The highest revision number allocated for this profile, or 0."""
        raw = self.metadata.get("LatestRevision")
        return int(raw) if raw is not None else 0

    @property
    def published_revision(self) -> Optional[int]:
        """The revision this record's body is, or ``None`` when that is not known.

        Not known for a profile saved before history began, and for one saved by
        a writer that keeps no history: in both cases the body may be one no
        revision holds, which is why the next save that does keep history
        records it before replacing it.
        """
        raw = self.metadata.get("PublishedRevision")
        return int(raw) if raw is not None else None


@dataclass(frozen=True)
class SaveOutcome:
    """What a save stored: the profile's new version and, if one, the new revision."""

    version: int
    #: ``None`` when the save changed nothing, so no revision was recorded.
    revision: Optional[int]


@dataclass(frozen=True)
class RevisionListing:
    """A profile's retained history, newest first, with the profile's counters."""

    profile: str
    #: The profile's current version: what a restore sends as its expected one.
    version: int
    latest_revision: int
    published_revision: Optional[int]
    entries: Tuple[RevisionEntry, ...]


@dataclass(frozen=True)
class RevisionRecord:
    """One revision: its metadata and the body it holds."""

    entry: RevisionEntry
    body: Dict[str, Any]


@dataclass(frozen=True)
class _RevisionPlan:
    """What a save records, and the counters it writes onto the profile.

    ``prior`` is the record the save replaces, when it has to be recorded first;
    ``new`` is the save's own revision, absent when the save changes nothing.
    """

    prior: Optional[Tuple[RevisionEntry, Dict[str, Any]]]
    new: Optional[Tuple[RevisionEntry, Dict[str, Any]]]
    latest: int
    published: Optional[int]


@dataclass(frozen=True)
class ResolvedDocument:
    """The baseline with the active profile merged over it."""

    body: Dict[str, Any]
    active_profile: Optional[str]
    default_version: int
    profile_version: int
    #: Whether ``Config#default`` was there to read. False means the table has
    #: not been seeded yet -- the window between a stack update putting new code
    #: live and the seeding custom resource running -- and the store deliberately
    #: does not cache such a document: caching it would hold the empty answer
    #: for the whole of a tier's TTL after seeding had finished.
    baseline_exists: bool = True


@dataclass(frozen=True)
class ResolvedSection:
    """One section of the resolved document, with how it was obtained."""

    name: str
    values: Dict[str, Any]
    fingerprint: str
    cache_tier: CacheTier
    served_from_cache: bool
    active_profile: Optional[str]


class ConfigurationStore:
    """Read and write runtime configuration.

    Build one per Lambda container at module scope: the cache lives on the
    instance, so a per-invocation instance would mean a per-invocation read
    whatever tier a section declares.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        *,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = _utc_now_iso,
        revision_bucket: Optional[str] = None,
        s3_client: Any = None,
        retained_revisions: int = DEFAULT_RETAINED_REVISIONS,
    ) -> None:
        """
        Args:
            table_name: The configuration table. Defaults to
                ``CONFIGURATION_TABLE_NAME``.
            revision_bucket: The bucket revision bodies are kept in. Defaults to
                ``CONFIGURATION_BUCKET_NAME``. With neither, the store keeps no
                history: every function that writes configuration is given the
                bucket, which a template test asserts, so this is the state of
                a reader and of a unit test, not of a deployed writer.
            s3_client: The S3 client to use for revisions; built when first
                needed otherwise.
            retained_revisions: How many revisions of a profile to keep.
        """
        self._table_name = table_name or os.environ.get(TABLE_NAME_ENV_VAR, "")
        if not self._table_name:
            raise ConfigurationTableNotConfigured(
                f"no configuration table name was supplied and {TABLE_NAME_ENV_VAR} is unset"
            )
        self._client = client if client is not None else boto3.client("dynamodb")
        self._clock = clock
        self._now = now
        self._cached: Optional[ResolvedDocument] = None
        self._cached_at: float = 0.0
        # Armed by `invalidate`; see it for why.
        self._read_consistently_next: bool = False
        bucket = (
            revision_bucket
            if revision_bucket is not None
            else os.environ.get(REVISION_BUCKET_ENV_VAR, "")
        )
        self._revisions: Optional[RevisionLog] = (
            RevisionLog(
                table_name=self._table_name,
                bucket=bucket,
                dynamodb=self._client,
                s3_factory=(lambda: s3_client) if s3_client is not None else _s3_client,
                now=now,
                retain=max(1, int(retained_revisions)),
            )
            if bucket
            else None
        )

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def keeps_history(self) -> bool:
        """Whether writes through this store record revisions."""
        return self._revisions is not None

    # ── reading ────────────────────────────────────────────────────────────

    def invalidate(self) -> None:
        """Forget the cached document, and read the next one consistently.

        Called after a write, so the writer's own next read sees what it saved,
        and called in production by the meeting-summary Lambda on the Regenerate
        path: the Meeting Summary panel promises the prompt templates as they
        are now, and dropping the cached copy is how that is kept.

        It arms a *one-shot* consistent read rather than changing anything
        durable. Both callers have the same problem: the read happens
        immediately after the thing it must observe, which is the one situation
        the tiers' eventual-consistency choice does not cover -- that choice
        rests on a TTL measured in tens of seconds being far longer than
        DynamoDB's replication lag, and there is no TTL here. One read, then back
        to the tier's own policy; still no way for a call site to choose a
        caching policy.
        """
        self._cached = None
        self._cached_at = 0.0
        self._read_consistently_next = True

    def get_section(self, name: str) -> ResolvedSection:
        """One section of the resolved document, honouring its declared tier.

        Raises:
            UnknownConfigurationSection: ``name`` is not registered, so no
                caching policy is declared for it.
        """
        spec = section_spec(name)
        document, from_cache = self._resolve(spec)
        values = document.body.get(spec.name, {})
        if not isinstance(values, dict):
            LOGGER.warning(
                "the configuration section %r holds a %s rather than an object; "
                "treating it as empty",
                spec.name,
                type(values).__name__,
            )
            values = {}
        return ResolvedSection(
            name=spec.name,
            values=values,
            fingerprint=_fingerprint(values),
            cache_tier=spec.cache_tier,
            served_from_cache=from_cache,
            active_profile=document.active_profile,
        )

    def get_document(self, *, consistent_read: bool = False) -> ResolvedDocument:
        """The whole resolved document, read now and not cached.

        For a caller that needs the whole document rather than one section --
        an export, or a diff against the baseline. Nothing in this stage calls
        it; `get_section` is the read path.
        """
        return self._fetch(consistent_read=consistent_read)

    def read_record(self, key: str, *, consistent_read: bool = False) -> ConfigurationRecord:
        """One stored record by its full partition key."""
        response = self._client.get_item(
            TableName=self._table_name,
            Key={codec.KEY_ATTRIBUTE: {"S": key}},
            ConsistentRead=consistent_read,
        )
        raw = response.get("Item")
        if not raw:
            return ConfigurationRecord(key=key, body={}, metadata={}, exists=False)
        item = codec.unmarshal_item(raw)
        metadata, body = codec.split_item(item)
        return ConfigurationRecord(key=key, body=body, metadata=metadata, exists=True)

    def read_profile(self, name: str, *, consistent_read: bool = False) -> ConfigurationRecord:
        """A profile by name, or the baseline when ``name`` is ``default``."""
        if name == DEFAULT_PROFILE_NAME:
            return self.read_record(DEFAULT_KEY, consistent_read=consistent_read)
        return self.read_record(profile_key(name), consistent_read=consistent_read)

    def active_profile_name(self, *, consistent_read: bool = False) -> Optional[str]:
        """The profile the pointer names, or ``None`` when there is no usable one.

        ``None`` for a pointer that is absent, empty, or naming something that
        is not a profile name. Only a hand edit can produce the last of those,
        and the answer is the same as for a pointer naming a profile that is not
        stored: fall back to the baseline. Raising instead would make one bad
        character in one attribute stop every read in the deployment, which for
        the prompt templates means no summaries at all.
        """
        record = self.read_record(ACTIVE_POINTER_KEY, consistent_read=consistent_read)
        active = record.metadata.get("ActiveProfile")
        if not active:
            return None
        name = str(active)
        if not is_profile_key(f"{CONFIG_PREFIX}{name}"):
            LOGGER.warning(
                "the active-profile pointer in %s names %r, which is not a profile "
                "name; using the deployment baseline",
                self._table_name,
                name,
            )
            return None
        return name

    # ── writing ────────────────────────────────────────────────────────────

    def write_default(self, body: Mapping[str, Any], *, description: str = "") -> None:
        """Write the deployment baseline.

        Unconditional, because CloudFormation is its only author and the
        baseline is meant to be rewritten by every stack update. Operator edits
        live in a profile, which this never touches.

        ``Managed`` marks the record as one a deployment owns, so a UI built on
        this table can show it read-only rather than letting an operator edit
        something the next release will replace.
        """
        metadata: Dict[str, Any] = {"Managed": True}
        if description:
            metadata["Description"] = description
        # Read first so `CreatedAt` keeps meaning when the record was first
        # written. Without it this rewrite would set it to now on every deploy,
        # leaving it indistinguishable from `UpdatedAt` and telling an operator
        # nothing. One extra read, once per seeder run.
        current = self.read_record(DEFAULT_KEY, consistent_read=True)
        self._put(
            DEFAULT_KEY,
            body,
            updated_by="cloudformation",
            # Advanced like a profile's, from the record read above, so that
            # `ResolvedDocument.default_version` distinguishes one deployment's
            # baseline from the next. Writing 1 every time would have left it
            # constant, which is a field that looks like information and is not.
            version=current.version + 1,
            created_at=current.metadata.get("CreatedAt"),
            extra_metadata=metadata,
        )
        self.invalidate()

    def write_profile(
        self,
        name: str,
        body: Mapping[str, Any],
        *,
        updated_by: str,
        expected_version: Optional[int],
        comment: Optional[str] = None,
    ) -> int:
        """Replace a whole profile body, refusing the write if the record moved on.

        Most callers want :meth:`write_section`, which changes one section and
        leaves the rest of the record alone. This replaces everything, so it is
        for a caller that owns the whole document -- an import, or a reset.

        Args:
            name: Profile name. Validated, so a reserved name is refused.
            body: The complete profile body to store.
            updated_by: The verified caller identity, recorded as ``UpdatedBy``
                and as the revision's author.
            expected_version: The version the caller read. ``None`` writes
                unconditionally and is for callers that have no version to
                offer; anything else is the concurrency guard.
            comment: Recorded on the revision this save creates, if any.

        Returns:
            The version now stored.

        Raises:
            ConcurrentModification: the stored version is not the expected one.
            ConfigurationTooLarge: the compressed record would not fit.
            RevisionHistoryUnavailable: the record this save would replace could
                not be recorded, so nothing was written.
        """
        return self._save(
            name,
            lambda _current: dict(body),
            updated_by=updated_by,
            expected_version=expected_version,
            comment=comment,
        ).version

    def write_section(
        self,
        name: str,
        section: str,
        values: Mapping[str, Any],
        *,
        updated_by: str,
        expected_version: Optional[int],
        comment: Optional[str] = None,
    ) -> int:
        """Save one section of a profile, leaving its other sections as they are.

        The operation a page that edits one thing wants. One record holds every
        section an operator has changed, so a page that replaced the whole body
        would delete the settings belonging to every other page -- which is not
        a problem while one section is registered, and is a silent one the day a
        second store moves in.

        The section is replaced outright rather than merged key by key: a key an
        operator removed has to be able to go, and "absent" is the only way to
        say that. The *profile* is still a delta over the baseline, which is a
        separate thing and is what the caller decides when it builds ``values``.

        Args:
            name: Profile name. Validated, so a reserved name is refused.
            section: The section to replace. Validated against the registry, so
                a typo cannot write a section nothing will ever read.
            values: The section's complete contents.
            updated_by: The verified caller identity, recorded as ``UpdatedBy``.
            expected_version: The version the caller read, as for
                :meth:`write_profile`. Unlike there, ``None`` does not make the
                write unconditional: a read-merge-write is pinned to the record
                it read either way. It decides only whether the caller also gets
                the pre-check that names who saved the record.
            comment: Recorded on the revision this save creates, if any.

        Returns:
            The version now stored.

        Raises:
            UnknownConfigurationSection: ``section`` is not registered.
            ConcurrentModification: the stored version is not the expected one.
            ConfigurationTooLarge: the compressed record would not fit.
            RevisionHistoryUnavailable: the record this save would replace could
                not be recorded, so nothing was written.
        """
        spec = section_spec(section)
        return self._save(
            name,
            lambda current: {**current.body, spec.name: dict(values)},
            updated_by=updated_by,
            expected_version=expected_version,
            # Conditional whatever the caller offered. This is a read, a merge
            # and a write, so an unconditional one is the shape that loses an
            # update: a save of a *different* section landing in between would
            # be overwritten by the body built from the earlier read.
            # `expected_version` only decides whether the caller also gets the
            # pre-check, which is what produces the message naming who saved.
            condition_on_read=True,
            comment=comment,
        ).version

    def _save(
        self,
        name: str,
        build_body: Callable[[ConfigurationRecord], Dict[str, Any]],
        *,
        updated_by: str,
        expected_version: Optional[int],
        condition_on_read: bool = False,
        comment: Optional[str] = None,
        source: str = SOURCE_SAVE,
        restored_from: Optional[int] = None,
    ) -> SaveOutcome:
        """One consistent read, then one whole-item write.

        ``build_body`` receives the record as it stands, so a caller that is
        changing part of it can merge rather than replace. Both the version
        check and the body are computed from that one read.

        ``condition_on_read`` pins the write to the record that read returned
        even when the caller offered no version. A caller that builds its body
        *from* the record needs that: without it, the write would replace
        whatever landed in between with a body computed before it existed.
        """
        key = profile_key(name)
        current = self.read_record(key, consistent_read=True)
        if expected_version is not None and current.version != expected_version:
            raise ConcurrentModification(
                f"the configuration profile {name!r} was saved by "
                f"{current.updated_by or 'someone else'} at "
                f"{current.updated_at or 'an unknown time'}; reload the page and "
                "apply the change again"
            )
        return self._commit(
            name,
            current,
            build_body(current),
            updated_by=updated_by,
            # Closes the window between the read above and this write. Applied
            # when the caller offered a version, and when the body was built
            # from the read (`condition_on_read`); only a whole-body replace
            # with no version offered is unconditional.
            condition=(
                self._condition_for(current)
                if (expected_version is not None or condition_on_read)
                else None
            ),
            comment=comment,
            source=source,
            restored_from=restored_from,
        )

    def _commit(
        self,
        name: str,
        current: ConfigurationRecord,
        body: Dict[str, Any],
        *,
        updated_by: str,
        condition: Optional[Mapping[str, Any]],
        comment: Optional[str] = None,
        source: str = SOURCE_SAVE,
        restored_from: Optional[int] = None,
    ) -> SaveOutcome:
        """Write ``body`` over ``current``, recording the revisions that implies.

        In three steps, in this order, for the rule in the module docstring:

        1. If ``current`` is not yet a recorded revision, record it -- refusing
           the save if that fails, before the profile has been written.
        2. Write the profile, carrying the revision counters, under
           ``condition``. Allocating the new revision's number here is what
           makes it exclusive: only one write can succeed from a given record.
        3. Store the new revision and prune. A failure here is logged, not
           raised: the save has happened, and the next save records this one
           from the profile before it replaces it.

        Nothing is ever stored under a number that a committed write did not
        allocate, which is what keeps two racing saves from storing different
        bodies under one number. Step 1 stores only the revision ``current``
        already is, and every writer reading ``current`` stores the same body
        for it; a record with no revision yet is first given one by
        :meth:`_start_history`, in a conditional write of its own.
        """
        attempts = 0
        while self._revisions is not None and current.exists and current.published_revision is None:
            if attempts == 2:
                # Twice another write got to the record first and left it with
                # no revision still: a writer that keeps no history, saving in a
                # loop. Refused rather than replacing a body nothing recorded.
                raise ConcurrentModification(
                    f"the configuration profile {name!r} kept changing while it was being "
                    "saved; reload the page and apply the change again"
                )
            attempts += 1
            _, current = self._start_history(name, current)
        now = self._now()
        next_version = current.version + 1
        plan = self._plan(name, current, body, updated_by, now, comment, source, restored_from)
        if plan.prior is not None:
            self._record_prior(name, *plan.prior)
        metadata: Dict[str, Any] = {}
        # Carried forward like CreatedAt, because a whole-item write would
        # otherwise drop it on the operator's first save -- which is exactly
        # when somebody might still need to know how the profile came to exist.
        if current.profile_origin:
            metadata["ProfileOrigin"] = current.profile_origin
        metadata.update(self._counters(plan.latest, plan.published))
        self._put(
            profile_key(name),
            body,
            updated_by=updated_by,
            version=next_version,
            created_at=current.metadata.get("CreatedAt"),
            updated_at=now,
            extra_metadata=metadata,
            condition=condition,
            profile_name=name,
        )
        self.invalidate()
        if plan.new is not None:
            self._record_new(name, *plan.new, published=plan.published)
        return SaveOutcome(
            version=next_version,
            revision=plan.new[0].revision if plan.new is not None else None,
        )

    def seed_profile(self, name: str, body: Mapping[str, Any], *, origin: str = "") -> bool:
        """Create a profile only if it is not there already.

        Args:
            name: Profile name. Validated, so a reserved name is refused.
            body: The body to store.
            origin: How the profile came to exist, stored as ``ProfileOrigin``
                and carried forward by every later write. For a seeder, this is
                the one durable record of whether an upgrade managed to bring an
                operator's settings across.

        Returns:
            True when this call created the record, False when one was already
            stored and was left as it was.

        This is what makes a stack update safe to re-run: the seeder calls it on
        every create *and* every update, and an operator's edits survive because
        the condition refuses rather than overwrites.

        With history kept, a created profile starts at revision 1 -- or after
        the highest revision its index still lists, for a profile recreated
        after being deleted by hand. Its number is written with the record, and
        its body stored only once the record is known to have been created, so
        a call that finds the profile already there writes no revision at all.
        """
        metadata: Dict[str, Any] = {"ProfileOrigin": origin} if origin else {}
        first = 0
        if self._revisions is not None:
            first = self._revisions.read_index(name).highest_revision + 1
            metadata.update(self._counters(first, first))
        now = self._now()
        created = self._put_if_absent(
            profile_key(name),
            body,
            profile_name=name,
            extra_metadata=metadata or None,
            updated_at=now,
        )
        if created and self._revisions is not None:
            entry = RevisionEntry(
                revision=first,
                created_at=now,
                created_by=_SEEDER,
                source=SOURCE_DEPLOY,
                version=1,
                sections=tuple(sorted(body)),
                size_bytes=_size_of(body),
            )
            self._record_new(name, entry, dict(body), published=first)
        return created

    def seed_section(self, name: str, section: str, values: Mapping[str, Any]) -> bool:
        """Add a section to a stored profile only if the profile does not hold it.

        The per-section counterpart of :meth:`seed_profile`, for a store that
        moves into this table after the profile already exists: the profile was
        created when the first store moved in, so "write the profile if it is
        absent" can no longer carry a later store's settings across.

        The section's *presence* is the guard. An empty section counts as
        present -- it is what the seeder writes when there was nothing to carry
        across, and what a page's "reset to defaults" saves -- so once a section
        is there, whether written here or by an administrator, no later run can
        replace it with older values.

        Args:
            name: Profile name. Validated, so a reserved name is refused.
            section: The section to add. Validated against the registry.
            values: The section's contents. May be empty.

        Returns:
            True when this call added the section; False when the profile is
            absent or already holds the section, in which case nothing is
            written.

        Raises:
            ConcurrentModification: the profile changed between the read and
                the write. Nothing is written; a caller can read again to see
                what landed.
        """
        spec = section_spec(section)
        current = self.read_record(profile_key(name), consistent_read=True)
        if not current.exists or spec.name in current.body:
            return False
        self._commit(
            name,
            current,
            {**current.body, spec.name: dict(values)},
            updated_by=_SEEDER,
            # Pinned to the read above: an administrator's save landing in
            # between must not be replaced by a body built before it existed.
            condition=self._condition_for(current),
            source=SOURCE_DEPLOY,
        )
        return True

    def set_active_profile(self, name: str, *, only_if_absent: bool = False) -> bool:
        """Point the active-profile sentinel at ``name``.

        Args:
            name: Profile name, validated like any other.
            only_if_absent: Leave an existing pointer alone. The seeder uses
                this so a stack update does not move an operator's choice.

        Returns:
            Whether the pointer was written.
        """
        validate_profile_name(name)
        metadata = {
            codec.KEY_ATTRIBUTE: ACTIVE_POINTER_KEY,
            "ActiveProfile": name,
            "UpdatedAt": self._now(),
            "Managed": True,
        }
        item = {key: codec.marshal_value(value) for key, value in metadata.items()}
        request: Dict[str, Any] = {"TableName": self._table_name, "Item": item}
        if only_if_absent:
            request["ConditionExpression"] = "attribute_not_exists(#key)"
            request["ExpressionAttributeNames"] = {"#key": codec.KEY_ATTRIBUTE}
        try:
            self._client.put_item(**request)
        except ClientError as error:
            if (
                only_if_absent
                and error.response.get("Error", {}).get("Code") == _CONDITIONAL_CHECK_FAILED
            ):
                return False
            raise
        self.invalidate()
        return True

    # ── history ────────────────────────────────────────────────────────────

    def _require_history(self) -> RevisionLog:
        if self._revisions is None:
            raise RevisionHistoryUnavailable(
                f"no revision bucket is configured ({REVISION_BUCKET_ENV_VAR} is unset), so "
                "this store keeps no configuration history"
            )
        return self._revisions

    def list_revisions(self, name: str) -> RevisionListing:
        """A profile's retained revisions, newest first.

        The published revision is always listed. If its body has not been
        stored yet -- its save could not store it, and no later save has run to
        record it -- it is listed from the profile record, as ``recovered``,
        because it is the profile's current state and the one thing a history
        view must not leave out.
        """
        log = self._require_history()
        head = self.read_profile(name, consistent_read=True)
        entries = list(log.read_index(name).entries)
        published = head.published_revision
        if head.exists and published is not None and all(e.revision != published for e in entries):
            entries.append(self._recovered_entry(head, published))
            entries.sort(key=lambda entry: entry.revision, reverse=True)
        return RevisionListing(
            profile=name,
            version=head.version,
            latest_revision=head.latest_revision,
            published_revision=published,
            entries=tuple(entries),
        )

    def get_revision(self, name: str, revision: int) -> RevisionRecord:
        """One retained revision, with its body.

        Raises:
            RevisionNotFound: the revision is not retained.
        """
        log = self._require_history()
        number = int(revision)
        entry = log.read_index(name).entry(number)
        envelope = log.read_body(name, number) if number > 0 else None
        if envelope is not None:
            return RevisionRecord(
                entry=entry or RevisionEntry.from_plain(envelope), body=dict(envelope["Body"])
            )
        # The published revision is the profile's current state, so its body is
        # the record's own when the object is not there yet.
        head = self.read_profile(name, consistent_read=True)
        if head.exists and head.published_revision == number:
            return RevisionRecord(
                entry=entry or self._recovered_entry(head, number), body=dict(head.body)
            )
        raise RevisionNotFound(
            f"revision {number} of the configuration profile {name!r} is not retained"
        )

    def diff_revisions(self, name: str, before: int, after: int) -> List[Dict[str, Any]]:
        """Every value that differs between two revisions; see ``diff_bodies``."""
        return diff_bodies(
            self.get_revision(name, before).body, self.get_revision(name, after).body
        )

    def restore_revision(
        self,
        name: str,
        revision: int,
        *,
        updated_by: str,
        expected_version: int,
        comment: Optional[str] = None,
    ) -> SaveOutcome:
        """Save an earlier revision's body as the profile's current one.

        Forward-only: the restored body is recorded as a *new* revision, so the
        state it replaces stays in the history and can itself be restored. A
        restore of the body the profile already holds records nothing, which
        the outcome's ``revision`` of ``None`` says.

        The whole profile is restored, every section of it, because that is
        what a revision is.

        Raises:
            RevisionNotFound: the revision is not retained.
            ConcurrentModification: the profile is no longer at
                ``expected_version``.
        """
        record = self.get_revision(name, revision)
        return self._save(
            name,
            lambda _current: dict(record.body),
            updated_by=updated_by,
            expected_version=int(expected_version),
            condition_on_read=True,
            comment=comment,
            source=SOURCE_RESTORE,
            restored_from=record.entry.revision,
        )

    def label_revision(self, name: str, revision: int, label: Optional[str]) -> bool:
        """Label a revision, or clear its label with ``None``.

        A labelled revision is kept however far the retention cap has moved on,
        which is what a label is for: marking a configuration worth returning to.

        Returns:
            Whether the revision was found and labelled.
        """
        return self._require_history().set_label(
            name, int(revision), clean_text(label, MAX_LABEL_LENGTH)
        )

    def ensure_revision_history(self, name: str) -> str:
        """Make sure the profile's current state is a recorded revision.

        What the deploy-time seeder calls, so that a deployment upgraded to a
        release with history starts it straight away -- the profile as it stands
        becomes revision 1 -- rather than at the next save. A save would do the
        same on its way through (see :meth:`_plan`); this only makes it happen
        at deploy time, and without counting as a save: the record's version,
        author and time are left as they are.

        Returns:
            ``started`` when the current state was recorded as the first
            revision; ``recovered`` when the published revision's body had not
            been stored and now has been; ``present`` when there was nothing to
            do; ``no-profile`` when the profile does not exist; and
            ``saved-concurrently`` when a save landed while this ran, which
            records the history itself.
        """
        log = self._require_history()
        current = self.read_profile(name, consistent_read=True)
        if not current.exists:
            return "no-profile"
        published = current.published_revision
        if published is None:
            outcome, _ = self._start_history(name, current)
            return outcome
        plan = self._plan(name, current, current.body, "", self._now(), None, SOURCE_SAVE, None)
        if plan.prior is not None:
            self._record_prior(name, *plan.prior)
            return "recovered"
        # Listed, but is the body there? Not if the bucket is not the one the
        # index was written against -- a table restored from a backup into a
        # stack whose bucket was replaced, say. The current state is the one
        # revision that can always be put back, because the record still
        # holds it.
        if log.read_body(name, published) is None:
            log.put_body(name, self._recovered_entry(current, published), current.body)
            return "recovered"
        return "present"

    # ── internals ──────────────────────────────────────────────────────────

    def _start_history(
        self, name: str, current: ConfigurationRecord
    ) -> Tuple[str, ConfigurationRecord]:
        """Give a record that is not yet a revision its revision number, then record it.

        For a profile saved before history began, or by a writer that keeps
        none. The number is written onto the record in a conditional write of
        its own, which changes the counters and nothing else: this is not a
        save, so the record's version, author and time stay as they are, and a
        page open on it is not told that somebody saved it. The body is stored
        only after that write has succeeded, so it is stored under a number
        nobody else can hold.

        Returns ``("started", record)`` with the record as it now stands, or
        ``("saved-concurrently", record)`` re-read, when another write got to
        the record first -- in which case that write did whatever recording was
        due.

        Raises:
            RevisionHistoryUnavailable: the number was allocated but the body
                could not be stored. Nothing is lost: the record still holds
                the body, and the next attempt records it from there.
        """
        log = self._require_history()
        first = max(current.latest_revision, log.read_index(name).highest_revision) + 1
        condition = self._condition_for(current)
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={codec.KEY_ATTRIBUTE: {"S": profile_key(name)}},
                UpdateExpression="SET #latest = :revision, #published = :revision",
                ConditionExpression=(
                    f"({condition['ConditionExpression']}) AND attribute_not_exists(#published)"
                ),
                ExpressionAttributeNames={
                    **condition["ExpressionAttributeNames"],
                    "#latest": "LatestRevision",
                    "#published": "PublishedRevision",
                },
                ExpressionAttributeValues={
                    **condition.get("ExpressionAttributeValues", {}),
                    ":revision": {"N": str(first)},
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != _CONDITIONAL_CHECK_FAILED:
                raise
            return "saved-concurrently", self.read_record(profile_key(name), consistent_read=True)
        self.invalidate()
        started = replace(
            current,
            metadata={**current.metadata, "LatestRevision": first, "PublishedRevision": first},
        )
        entry = RevisionEntry(
            revision=first,
            created_at=current.updated_at,
            created_by=current.updated_by,
            source=SOURCE_BACKFILL,
            version=current.version,
            sections=tuple(sorted(current.body)),
            size_bytes=_size_of(current.body),
        )
        self._record_prior(name, entry, current.body)
        return "started", started

    @staticmethod
    def _counters(latest: int, published: Optional[int]) -> Dict[str, Any]:
        counters: Dict[str, Any] = {}
        if latest:
            counters["LatestRevision"] = latest
        if published is not None:
            counters["PublishedRevision"] = published
        return counters

    @staticmethod
    def _recovered_entry(head: ConfigurationRecord, revision: int) -> RevisionEntry:
        """An entry for the revision a record *is*, made from the record itself."""
        return RevisionEntry(
            revision=revision,
            created_at=head.updated_at,
            created_by=head.updated_by,
            source=SOURCE_RECOVERED,
            version=head.version,
            size_bytes=_size_of(head.body),
        )

    def _plan(
        self,
        name: str,
        current: ConfigurationRecord,
        body: Mapping[str, Any],
        updated_by: str,
        now: str,
        comment: Optional[str],
        source: str,
        restored_from: Optional[int],
    ) -> _RevisionPlan:
        """Which revisions a write of ``body`` over ``current`` records.

        Without history the counters are still carried, because the profile is
        written as a whole item and a write that dropped ``LatestRevision``
        would let the next writer that does keep history allocate a number it
        had already used. ``PublishedRevision`` is carried only while the body
        is unchanged: once a write that kept no history has changed it, no
        revision holds the body any more, and the next write that keeps history
        has to record it before replacing it.
        """
        unchanged = current.exists and codec.encode_body(body) == codec.encode_body(current.body)
        if self._revisions is None:
            return _RevisionPlan(
                prior=None,
                new=None,
                latest=current.latest_revision,
                published=current.published_revision if unchanged else None,
            )

        index = self._revisions.read_index(name)
        # Above the index as well as the counter. They agree except where the
        # record has been rewritten without its counters while the index
        # survived -- by code that keeps no history and predates the counters, or a
        # profile deleted by hand and recreated -- and a number the index
        # already lists must not be given to a second body.
        latest = max(current.latest_revision, index.highest_revision)
        published = current.published_revision
        prior: Optional[Tuple[RevisionEntry, Dict[str, Any]]] = None
        if current.exists and published is not None and index.entry(published) is None:
            # Its number was allocated, but its body was not stored when it was
            # saved. The record still holds that body, so it is recorded from
            # the record, under the number it was given. (A record with no
            # number at all has been given one by `_start_history` already.)
            prior = (self._recovered_entry(current, published), current.body)

        new: Optional[Tuple[RevisionEntry, Dict[str, Any]]] = None
        if not unchanged:
            # Suppressed when the body is byte-for-byte what is stored: a
            # deployment, or a page saved without an edit, must not push real
            # history out of the retention window.
            latest += 1
            published = latest
            new = (
                RevisionEntry(
                    revision=latest,
                    created_at=now,
                    created_by=updated_by,
                    source=source,
                    version=current.version + 1,
                    sections=tuple(
                        changed_sections(current.body, body) if current.exists else sorted(body)
                    ),
                    comment=clean_text(comment, MAX_COMMENT_LENGTH),
                    restored_from=restored_from,
                    size_bytes=_size_of(body),
                ),
                dict(body),
            )
        return _RevisionPlan(prior=prior, new=new, latest=latest, published=published)

    def _record_prior(self, name: str, entry: RevisionEntry, body: Mapping[str, Any]) -> None:
        """Record the revision a record already is, or refuse to replace it.

        Idempotent, which is what makes it safe to run before the write's own
        condition has been checked: the number is one a committed write gave
        the record, and every record holding that number holds the same body,
        so two writers racing store the same object, and the index keeps one
        entry per number.
        """
        log = self._require_history()
        try:
            stored = (
                log.read_body(name, entry.revision) if entry.source == SOURCE_RECOVERED else None
            )
            if stored is not None:
                # Stored after all; only its index entry is missing.
                entry = RevisionEntry.from_plain(stored)
            else:
                log.put_body(name, entry, body)
            if log.read_index(name).entry(entry.revision) is None:
                log.append(name, [entry])
        except (ClientError, BotoCoreError, ConfigurationError) as error:
            raise RevisionHistoryUnavailable(
                f"the current state of the configuration profile {name!r} (revision "
                f"{entry.revision}) could not be recorded in its history, so it was not "
                f"replaced and the save was not made: {error}"
            ) from error

    def _record_new(
        self,
        name: str,
        entry: RevisionEntry,
        body: Mapping[str, Any],
        *,
        published: Optional[int],
    ) -> None:
        """Store a revision whose write has succeeded, then prune.

        Logged rather than raised on failure: the write has happened and cannot
        be taken back, and the record now holds this body, so the next save
        records it from the record before replacing it (``recovered``).
        """
        log = self._revisions
        if log is None:
            return
        try:
            log.put_body(name, entry, body)
            log.append(name, [entry])
        except (ClientError, BotoCoreError, ConfigurationError):
            LOGGER.exception(
                "saved the configuration profile %r, but could not record revision %d; "
                "the next save records it from the profile",
                name,
                entry.revision,
            )
            return
        try:
            log.prune(name, protected=(published,) if published is not None else ())
        except (ClientError, BotoCoreError, ConfigurationError):
            LOGGER.exception("could not prune the revision history of %r", name)

    def _resolve(self, spec: SectionSpec) -> tuple[ResolvedDocument, bool]:
        """The resolved document, from the cache when the tier allows it."""
        ttl = TIER_TTL_SECONDS[spec.cache_tier]
        if ttl > 0 and self._cached is not None and (self._clock() - self._cached_at) < ttl:
            return self._cached, True
        consistent = TIER_CONSISTENT_READ[spec.cache_tier] or self._read_consistently_next
        self._read_consistently_next = False
        document = self._fetch(consistent_read=consistent)
        if document.baseline_exists:
            self._cached = document
            self._cached_at = self._clock()
        else:
            # Nothing to cache yet. A stack update can put new code live before
            # the seeding custom resource has written the baseline, and caching
            # the empty document would keep every reader answering from it for
            # the whole of the tier's TTL after seeding had finished. Reading
            # again each time until the baseline appears makes that window as
            # short as it can be.
            LOGGER.warning(
                "no %s record in %s; the configuration seeder has not run yet or "
                "its records were removed. Not caching this read.",
                DEFAULT_KEY,
                self._table_name,
            )
            self.invalidate()
        return document, False

    def _fetch(self, *, consistent_read: bool) -> ResolvedDocument:
        """Read the baseline and the active profile and merge them."""
        active = self.active_profile_name(consistent_read=consistent_read)
        default = self.read_record(DEFAULT_KEY, consistent_read=consistent_read)
        profile = ConfigurationRecord(key="", body={}, metadata={}, exists=False)
        if active:
            profile = self.read_record(profile_key(active), consistent_read=consistent_read)
        return ResolvedDocument(
            body=deep_merge(default.body, profile.body),
            active_profile=active if profile.exists else None,
            default_version=default.version,
            profile_version=profile.version,
            baseline_exists=default.exists,
        )

    def _item(
        self,
        key: str,
        body: Mapping[str, Any],
        *,
        updated_by: str,
        version: int,
        created_at: Optional[str],
        extra_metadata: Optional[Mapping[str, Any]] = None,
        updated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = updated_at or self._now()
        compressed = codec.encode_body(body)
        metadata: Dict[str, Any] = {
            codec.KEY_ATTRIBUTE: key,
            "CreatedAt": created_at or now,
            "UpdatedAt": now,
            "UpdatedBy": updated_by,
            "Version": version,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        codec.check_item_size(key, compressed, metadata)
        item = {name: codec.marshal_value(value) for name, value in metadata.items()}
        item[codec.BODY_ATTRIBUTE] = {"B": compressed}
        return item

    @staticmethod
    def _condition_for(current: ConfigurationRecord) -> Dict[str, Any]:
        """The condition that pins a write to the record the caller just read.

        Three cases, because "no version" and "no record" are not the same
        thing. Every record this package writes carries a ``Version``; one
        without it is an inline record -- lifted into this table by a later
        store migration, or repaired by hand in the console -- and its version
        reads as 0, the same number as an absent record. Conditioning that write
        on the record being absent would refuse the save outright, so it is
        conditioned instead on the record still being un-versioned, which is
        equally specific and equally safe: a concurrent save converts it, and
        the second save is then refused.
        """
        if not current.exists:
            return {
                "ConditionExpression": "attribute_not_exists(#key)",
                "ExpressionAttributeNames": {"#key": codec.KEY_ATTRIBUTE},
            }
        if current.version == 0:
            return {
                "ConditionExpression": "attribute_not_exists(#version)",
                "ExpressionAttributeNames": {"#version": "Version"},
            }
        return {
            "ConditionExpression": "#version = :expected",
            "ExpressionAttributeNames": {"#version": "Version"},
            "ExpressionAttributeValues": {":expected": {"N": str(current.version)}},
        }

    def _put(
        self,
        key: str,
        body: Mapping[str, Any],
        *,
        updated_by: str,
        version: int,
        created_at: Optional[str] = None,
        condition: Optional[Mapping[str, Any]] = None,
        extra_metadata: Optional[Mapping[str, Any]] = None,
        profile_name: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        """Write a whole record, optionally conditional on its current state.

        A whole-item write rather than an update, so that converting a record
        from the inline layout leaves none of its old top-level attributes
        behind. The condition is what keeps it from being a last-save-wins
        write.
        """
        item = self._item(
            key,
            body,
            updated_by=updated_by,
            version=version,
            created_at=created_at,
            extra_metadata=extra_metadata,
            updated_at=updated_at,
        )
        request: Dict[str, Any] = {"TableName": self._table_name, "Item": item}
        if condition is not None:
            request.update(condition)
        try:
            self._client.put_item(**request)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == _CONDITIONAL_CHECK_FAILED:
                raise ConcurrentModification(
                    f"the configuration profile {profile_name or key!r} changed "
                    "while it was being saved; reload the page and apply the "
                    "change again"
                ) from error
            raise

    def _put_if_absent(
        self,
        key: str,
        body: Mapping[str, Any],
        *,
        profile_name: Optional[str] = None,
        extra_metadata: Optional[Mapping[str, Any]] = None,
        updated_at: Optional[str] = None,
    ) -> bool:
        item = self._item(
            key,
            body,
            updated_by=_SEEDER,
            version=1,
            created_at=None,
            extra_metadata=extra_metadata,
            updated_at=updated_at,
        )
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(#key)",
                ExpressionAttributeNames={"#key": codec.KEY_ATTRIBUTE},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == _CONDITIONAL_CHECK_FAILED:
                LOGGER.info(
                    "the configuration profile %r is already stored; leaving it unchanged",
                    profile_name or key,
                )
                return False
            raise
        self.invalidate()
        return True
