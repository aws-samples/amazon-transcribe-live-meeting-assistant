#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Where Amazon Bedrock runs a model: the Model inference geography setting.

Amazon Bedrock offers most models LMA uses through inference profiles, and the
profile's prefix decides which AWS Regions may serve a request:

* ``global.`` routes to any commercial AWS Region;
* a geography prefix (``us.``, ``eu.``, ``apac.``, and the country profiles
  ``jp.``, ``au.``, ``ca.``, ``in.``) routes only within that geography;
* a plain model id (``amazon.nova-lite-v1:0``) runs in the region it is called
  in, where Amazon Bedrock offers it there on demand.

The Amazon Bedrock Models section's ``inferenceGeography`` setting chooses
between three behaviours, applied to every model id LMA calls Amazon Bedrock
with:

``global`` (the default)
    Every model id is used as chosen. This is the behaviour of every release
    before the setting existed.
``geography``
    Inference stays within the geography of the region the call is made from.
    A model id already within it (a profile of that geography, or a plain
    in-region id) is used as chosen. Any other id -- a ``global.`` profile, or
    another geography's -- is run as the same model through the first of the
    geography's profiles that Amazon Bedrock offers for it, narrowest first,
    else as the in-region model id. A plain id the region does not offer on
    demand is run the same way.
``in_region``
    Inference stays in the region the call is made from: any profile id is run
    as the plain in-region model id.

The setting decides where a model runs; the model settings decide which model.
So a model chosen on the Configuration page is never swapped for a different
model, only addressed through a different profile.

**When the geography has no id for the model**, the call is refused with
:class:`ModelUnavailableInGeography`, which names the setting and the model.
The alternative of keeping the ``global.`` id with a logged warning was
rejected: the setting exists for deployments that need inference to stay in a
geography, and a fallback that leaves it would make the setting's promise
depend on log monitoring. The Configuration page shows, before a change is
saved, which features have no model in the selected geography.

For the same reason, a configuration table that is configured but cannot be
read raises :class:`InferenceGeographyUnreadable` rather than reading as
``global``, whatever the setting holds; this includes deployments that never
saved the setting, because the read fails before its value is known. Only a
deployment with no configuration table, or one whose table is read and has no
saved setting, runs as ``global``.

Which ids exist comes from ``bedrock_inference_catalog.json`` beside this
module, rebuilt from the Amazon Bedrock listing APIs by
``scripts/bedrock_inference_catalog.py``. For a region the catalogue has no
entry for, or a model it does not list, the id is built by rule (the
geography's first, narrowest, profile prefix, else the plain id) and used
unchecked, with a warning; that id either works or fails, and in neither
case does inference leave the geography.

Two ports of :func:`resolve_for_geography` exist, each with a byte-identical
copy of the catalogue: the Configuration page's
(``ui/src/components/configuration/inference-geography.js``), for its preview,
and the Virtual Participant's (``backend/src/inference-geography.ts``), for its
page-automation model. ``lma-ai-stack/test/test_inference_geography.py`` holds
the copies identical and writes the table of cases both ports are tested
against from this module's answers.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Mapping, Optional, Set, Tuple

LOGGER = logging.getLogger(__name__)

CATALOG_FILE = Path(__file__).resolve().parent / "bedrock_inference_catalog.json"

#: The section and setting that hold the choice.
GEOGRAPHY_SECTION = "language_models"
GEOGRAPHY_SETTING = "inferenceGeography"

GLOBAL = "global"
GEOGRAPHY = "geography"
IN_REGION = "in_region"
GEOGRAPHIES = (GLOBAL, GEOGRAPHY, IN_REGION)


class ModelUnavailableInGeography(ValueError):
    """The selected inference geography offers no id for the chosen model.

    A ``ValueError`` rather than a :class:`~lma_config.errors.ConfigurationError`
    on purpose: ``read_setting`` treats a configuration error as "use the shipped
    default", and the shipped default is a ``global.`` id.
    """


class InferenceGeographyUnreadable(RuntimeError):
    """The configuration table is configured, and reading the setting from it failed.

    Raised instead of reading the setting as ``global``, which would run the call
    outside a geography an administrator may have chosen. Not a
    :class:`~lma_config.errors.ConfigurationError`, for the reason
    :class:`ModelUnavailableInGeography` gives.
    """


@dataclass(frozen=True)
class ModelResolution:
    """How one model id is run under one geography, from one region."""

    requested: str
    model_id: str
    geography: str
    region: str
    #: Whether the catalogue lists ``model_id`` for ``region``; False when the id
    #: was built by rule or left as chosen without a check.
    verified: bool

    @property
    def changed(self) -> bool:
        return self.model_id != self.requested


@lru_cache(maxsize=1)
def catalog() -> Mapping[str, Any]:
    """The shipped catalogue."""
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def profile_prefixes(data: Optional[Mapping[str, Any]] = None) -> Tuple[str, ...]:
    """Every inference-profile prefix, ``global`` included."""
    return tuple((data or catalog())["profilePrefixes"])


def split_model_id(
    model_id: str, data: Optional[Mapping[str, Any]] = None
) -> Tuple[Optional[str], str]:
    """``(profile prefix, bare model id)``; the prefix is None for a plain model id."""
    head, _, rest = (model_id or "").partition(".")
    if rest and head in profile_prefixes(data):
        return head, rest
    return None, model_id


def base_model_id(model_id: str) -> str:
    """The model id with any inference-profile prefix removed."""
    return split_model_id(model_id)[1]


def is_inference_profile(model_id: str) -> bool:
    """Whether ``model_id`` names an inference profile rather than a foundation model."""
    return split_model_id(model_id)[0] is not None


def partition_of(region: str) -> str:
    """The AWS partition ``region`` belongs to (``aws``, ``aws-us-gov``, ``aws-cn``)."""
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def model_arn(model_id: str, region: str, account_id: str, partition: Optional[str] = None) -> str:
    """The ARN of ``model_id`` as a knowledge base's ``modelArn`` takes it.

    An inference profile lives in the calling account; a foundation model is in
    the region's model catalogue, which has no account in its ARN. ``partition``
    defaults to ``region``'s.
    """
    partition = partition or partition_of(region)
    if is_inference_profile(model_id):
        return f"arn:{partition}:bedrock:{region}:{account_id}:inference-profile/{model_id}"
    return f"arn:{partition}:bedrock:{region}::foundation-model/{model_id}"


def geography_prefixes(region: str, data: Optional[Mapping[str, Any]] = None) -> Tuple[str, ...]:
    """The profile prefixes within ``region``'s geography, narrowest first; empty for none."""
    for entry in (data or catalog())["geographies"]:
        match = entry["match"]
        if region == match or (match.endswith("-") and region.startswith(match)):
            return tuple(entry["prefixes"])
    return ()


def _offered(region: str, data: Mapping[str, Any]) -> Tuple[bool, Set[str]]:
    """Whether the catalogue lists ``region``, and the ids it lists there."""
    listed = data["regions"].get(region)
    if listed is None:
        return False, set()
    return True, set(listed.get("profiles", [])) | set(listed.get("inRegion", []))


def resolve_for_geography(
    model_id: str,
    geography: str,
    region: str,
    data: Optional[Mapping[str, Any]] = None,
) -> ModelResolution:
    """How ``model_id`` is run under ``geography`` when called from ``region``.

    Pure: reads nothing but the catalogue. See the module docstring for the rules.

    Raises:
        ModelUnavailableInGeography: the catalogue lists ``region`` and ``model_id``'s
            model, and none of the ids the geography allows is offered there.
    """
    data = data or catalog()
    if geography not in (GEOGRAPHY, IN_REGION) or not model_id:
        return ModelResolution(model_id, model_id, GLOBAL, region, verified=False)

    prefix, base = split_model_id(model_id, data)
    region_listed, offered = _offered(region, data)
    known = region_listed and base in set(data.get("models", []))
    allowed = geography_prefixes(region, data) if geography == GEOGRAPHY else ()

    def keep() -> ModelResolution:
        return ModelResolution(
            model_id, model_id, geography, region, verified=bool(known and model_id in offered)
        )

    if prefix is None:
        # A plain id already runs in-region. Under the geography setting one the
        # region does not offer on demand is run through the geography's profile
        # of the same model instead, if there is one.
        if geography == IN_REGION or not known or model_id in offered:
            return keep()
        for candidate in (f"{p}.{base}" for p in allowed):
            if candidate in offered:
                return ModelResolution(model_id, candidate, geography, region, verified=True)
        return keep()

    if prefix in allowed:
        return keep()

    candidates: List[str] = [f"{p}.{base}" for p in allowed] + [base]
    if not known:
        return ModelResolution(model_id, candidates[0], geography, region, verified=False)
    for candidate in candidates:
        if candidate in offered:
            return ModelResolution(model_id, candidate, geography, region, verified=True)
    raise ModelUnavailableInGeography(
        f"{model_id} has no Amazon Bedrock id that keeps inference "
        f"{'in ' + region if geography == IN_REGION else 'within the geography of ' + region} "
        f"(Model inference geography: {geography}). Choose another model in the Amazon Bedrock "
        "Models section of the Configuration page, or another geography."
    )


#: (requested, geography, region) already logged in this process.
_LOGGED: set = set()


def _log_once(resolution: ModelResolution) -> None:
    key = (resolution.requested, resolution.geography, resolution.region)
    if key in _LOGGED or (not resolution.changed and resolution.verified):
        return
    _LOGGED.add(key)
    if not resolution.verified:
        LOGGER.warning(
            "Model inference geography %s: %s is run as %s from %s, which the shipped "
            "catalogue of Amazon Bedrock ids does not list, so whether it is offered there "
            "was not checked",
            resolution.geography,
            resolution.requested,
            resolution.model_id,
            resolution.region,
        )
    elif resolution.changed:
        LOGGER.info(
            "Model inference geography %s: %s is run as %s from %s",
            resolution.geography,
            resolution.requested,
            resolution.model_id,
            resolution.region,
        )


def configured_geography(store: Any = None) -> str:
    """The saved Model inference geography.

    ``global`` when there is no configuration table or the setting is unset (or
    holds a value that is not a geography).

    Raises:
        InferenceGeographyUnreadable: the configuration table is configured and
            reading the section from it failed.
    """
    # Imported here: parameters imports the store, which this module must not
    # need for its pure functions (the Configuration page's tests load them).
    # pylint: disable=import-outside-toplevel
    from botocore.exceptions import BotoCoreError, ClientError

    from .errors import ConfigurationError, ConfigurationTableNotConfigured
    from .parameters import _shared_store  # pylint: disable=protected-access

    reader = store if store is not None else _shared_store()
    if reader is None:
        return GLOBAL
    try:
        values = reader.get_section(GEOGRAPHY_SECTION).values
    except ConfigurationTableNotConfigured:
        return GLOBAL
    except (ClientError, BotoCoreError, ConfigurationError) as error:
        raise InferenceGeographyUnreadable(
            f"could not read the Model inference geography (the {GEOGRAPHY_SECTION} "
            f"configuration section): {error}. The Amazon Bedrock call is not made, so "
            "that it cannot run outside the geography the setting may hold."
        ) from error
    value = values.get(GEOGRAPHY_SETTING)
    return value if value in GEOGRAPHIES else GLOBAL


def resolve_model_id(model_id: str, *, region: str = "", store: Any = None) -> str:
    """The id to call Amazon Bedrock with for ``model_id``, under the saved geography.

    Every LMA reader of a model setting passes the value through this, with the
    region its Amazon Bedrock client calls (``AWS_REGION`` when not given).

    Raises:
        ModelUnavailableInGeography: see :func:`resolve_for_geography`.
        InferenceGeographyUnreadable: see :func:`configured_geography`.
    """
    geography = configured_geography(store)
    if geography == GLOBAL or not model_id:
        return model_id
    resolution = resolve_for_geography(
        model_id, geography, region or os.environ.get("AWS_REGION", "")
    )
    _log_once(resolution)
    return resolution.model_id


def price_stand_ins(service: str) -> List[str]:
    """Price-table entries to use for a Bedrock ``service`` the table does not list.

    ``service`` is a metering service name, ``bedrock/<model id>``. The price
    table lists the ``global.`` and ``us.`` profiles and the plain Nova ids; an id
    the geography setting produces that it does not list (``eu.``, ``jp.``, a
    plain Claude id) is priced as the same model's global profile, then its plain
    id, then its ``us.`` profile, and the estimate line says so.
    """
    head, _, model_id = service.partition("/")
    if head != "bedrock" or not model_id:
        return []
    base = base_model_id(model_id)
    stand_ins = [f"global.{base}", base, f"us.{base}"]
    return [f"bedrock/{m}" for m in stand_ins if m != model_id]
