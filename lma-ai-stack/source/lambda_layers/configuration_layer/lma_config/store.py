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
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

import boto3
from botocore.exceptions import ClientError

from . import codec
from .errors import (
    ConcurrentModification,
    ConfigurationTableNotConfigured,
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    ) -> None:
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

    @property
    def table_name(self) -> str:
        return self._table_name

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
    ) -> int:
        """Replace a whole profile body, refusing the write if the record moved on.

        Most callers want :meth:`write_section`, which changes one section and
        leaves the rest of the record alone. This replaces everything, so it is
        for a caller that owns the whole document -- an import, or a reset.

        Args:
            name: Profile name. Validated, so a reserved name is refused.
            body: The complete profile body to store.
            updated_by: The verified caller identity, recorded as ``UpdatedBy``.
            expected_version: The version the caller read. ``None`` writes
                unconditionally and is for callers that have no version to
                offer; anything else is the concurrency guard.

        Returns:
            The version now stored.

        Raises:
            ConcurrentModification: the stored version is not the expected one.
            ConfigurationTooLarge: the compressed record would not fit.
        """
        return self._save(
            name,
            lambda _current: dict(body),
            updated_by=updated_by,
            expected_version=expected_version,
        )

    def write_section(
        self,
        name: str,
        section: str,
        values: Mapping[str, Any],
        *,
        updated_by: str,
        expected_version: Optional[int],
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

        Returns:
            The version now stored.

        Raises:
            UnknownConfigurationSection: ``section`` is not registered.
            ConcurrentModification: the stored version is not the expected one.
            ConfigurationTooLarge: the compressed record would not fit.
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
        )

    def _save(
        self,
        name: str,
        build_body: Callable[[ConfigurationRecord], Dict[str, Any]],
        *,
        updated_by: str,
        expected_version: Optional[int],
        condition_on_read: bool = False,
    ) -> int:
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
        next_version = current.version + 1
        # Carried forward like CreatedAt, because a whole-item write would
        # otherwise drop it on the operator's first save -- which is exactly
        # when somebody might still need to know how the profile came to exist.
        origin = current.profile_origin
        self._put(
            key,
            build_body(current),
            updated_by=updated_by,
            version=next_version,
            created_at=current.metadata.get("CreatedAt"),
            extra_metadata={"ProfileOrigin": origin} if origin else None,
            # Closes the window between the read above and this write. Applied
            # when the caller offered a version, and when the body was built
            # from the read (`condition_on_read`); only a whole-body replace
            # with no version offered is unconditional.
            condition=(
                self._condition_for(current)
                if (expected_version is not None or condition_on_read)
                else None
            ),
            profile_name=name,
        )
        self.invalidate()
        return next_version

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
        """
        return self._put_if_absent(
            profile_key(name),
            body,
            profile_name=name,
            extra_metadata={"ProfileOrigin": origin} if origin else None,
        )

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

    # ── internals ──────────────────────────────────────────────────────────

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
    ) -> Dict[str, Any]:
        now = self._now()
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
    ) -> bool:
        item = self._item(
            key,
            body,
            updated_by="cloudformation",
            version=1,
            created_at=None,
            extra_metadata=extra_metadata,
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
