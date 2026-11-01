#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""LMA's shared runtime-configuration accessor.

One table, one accessor, and a caching policy declared per section rather than
chosen per call site. See :mod:`lma_config.keys` for the key layout,
:mod:`lma_config.codec` for how a record is stored, :mod:`lma_config.sections`
for the cache tiers and `docs/configuration-store.md` for the whole picture.

The TypeScript counterpart lives in
``lma-websocket-transcriber-stack/source/app/src/config/`` and implements the
same key layout, body encoding and tiers.
"""

from .codec import (
    BODY_ATTRIBUTE,
    KEY_ATTRIBUTE,
    MAX_ITEM_BYTES,
    METADATA_ATTRIBUTES,
    WARN_ITEM_BYTES,
)
from .errors import (
    ConcurrentModification,
    ConfigurationError,
    ConfigurationTableNotConfigured,
    ConfigurationTooLarge,
    InvalidConfiguration,
    InvalidProfileName,
    ReservedProfileName,
    RevisionHistoryUnavailable,
    RevisionNotFound,
    UnknownConfigurationSection,
)
from .keys import (
    ACTIVE_POINTER_KEY,
    CONFIG_PREFIX,
    DEFAULT_KEY,
    DEFAULT_PROFILE_NAME,
    RESERVED_PROFILE_NAMES,
    REVISION_INDEX_PREFIX,
    SCHEMA_KEY,
    is_profile_key,
    profile_key,
    validate_profile_name,
)
from .parameters import (
    ParameterDefault,
    apply_parameter_defaults,
    parameter_defaults,
    read_setting,
)
from .presets import InvalidPreset, shipped_presets
from .revisions import (
    DEFAULT_RETAINED_REVISIONS,
    MAX_COMMENT_LENGTH,
    MAX_LABEL_LENGTH,
    REVISION_BUCKET_ENV_VAR,
    REVISION_S3_PREFIX,
    RevisionEntry,
    diff_bodies,
)
from .schema import section_schema, section_schemas, validate_section
from .sections import (
    ASR_ENGINE,
    CALL_PRICING,
    CHAT_BUTTONS,
    LANGUAGE_MODELS,
    MEETING_ASSIST,
    MEETING_PROCESSING,
    NOVA_SONIC_CONFIG,
    RECORDING,
    SUMMARY_PROMPT_TEMPLATES,
    TIER_CONSISTENT_READ,
    TIER_TTL_SECONDS,
    TRANSCRIPTION,
    CacheTier,
    SectionSpec,
    register,
    registered_sections,
    section_spec,
)
from .store import (
    TABLE_NAME_ENV_VAR,
    ConfigurationRecord,
    ConfigurationStore,
    ResolvedDocument,
    ResolvedSection,
    RevisionListing,
    RevisionRecord,
    SaveOutcome,
    deep_merge,
)

__all__ = [
    "ACTIVE_POINTER_KEY",
    "ASR_ENGINE",
    "BODY_ATTRIBUTE",
    "CHAT_BUTTONS",
    "CALL_PRICING",
    "CONFIG_PREFIX",
    "DEFAULT_KEY",
    "DEFAULT_RETAINED_REVISIONS",
    "DEFAULT_PROFILE_NAME",
    "KEY_ATTRIBUTE",
    "LANGUAGE_MODELS",
    "MEETING_ASSIST",
    "MEETING_PROCESSING",
    "MAX_COMMENT_LENGTH",
    "MAX_ITEM_BYTES",
    "MAX_LABEL_LENGTH",
    "METADATA_ATTRIBUTES",
    "NOVA_SONIC_CONFIG",
    "RECORDING",
    "RESERVED_PROFILE_NAMES",
    "REVISION_BUCKET_ENV_VAR",
    "REVISION_INDEX_PREFIX",
    "REVISION_S3_PREFIX",
    "SCHEMA_KEY",
    "SUMMARY_PROMPT_TEMPLATES",
    "TABLE_NAME_ENV_VAR",
    "TIER_CONSISTENT_READ",
    "TIER_TTL_SECONDS",
    "TRANSCRIPTION",
    "WARN_ITEM_BYTES",
    "CacheTier",
    "ConcurrentModification",
    "ConfigurationError",
    "ConfigurationRecord",
    "ConfigurationStore",
    "ConfigurationTableNotConfigured",
    "ConfigurationTooLarge",
    "InvalidConfiguration",
    "InvalidPreset",
    "InvalidProfileName",
    "ParameterDefault",
    "ReservedProfileName",
    "ResolvedDocument",
    "ResolvedSection",
    "RevisionEntry",
    "RevisionHistoryUnavailable",
    "RevisionListing",
    "RevisionNotFound",
    "RevisionRecord",
    "SaveOutcome",
    "SectionSpec",
    "UnknownConfigurationSection",
    "apply_parameter_defaults",
    "deep_merge",
    "diff_bodies",
    "is_profile_key",
    "parameter_defaults",
    "profile_key",
    "read_setting",
    "register",
    "registered_sections",
    "section_schema",
    "section_schemas",
    "section_spec",
    "shipped_presets",
    "validate_profile_name",
    "validate_section",
]
