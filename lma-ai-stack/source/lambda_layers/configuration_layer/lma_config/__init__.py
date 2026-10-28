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
    InvalidProfileName,
    ReservedProfileName,
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
from .sections import (
    CALL_PRICING,
    CHAT_BUTTONS,
    NOVA_SONIC_CONFIG,
    SUMMARY_PROMPT_TEMPLATES,
    TIER_CONSISTENT_READ,
    TIER_TTL_SECONDS,
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
    deep_merge,
)

__all__ = [
    "ACTIVE_POINTER_KEY",
    "BODY_ATTRIBUTE",
    "CHAT_BUTTONS",
    "CALL_PRICING",
    "CONFIG_PREFIX",
    "DEFAULT_KEY",
    "DEFAULT_PROFILE_NAME",
    "KEY_ATTRIBUTE",
    "MAX_ITEM_BYTES",
    "METADATA_ATTRIBUTES",
    "NOVA_SONIC_CONFIG",
    "RESERVED_PROFILE_NAMES",
    "REVISION_INDEX_PREFIX",
    "SCHEMA_KEY",
    "SUMMARY_PROMPT_TEMPLATES",
    "TABLE_NAME_ENV_VAR",
    "TIER_CONSISTENT_READ",
    "TIER_TTL_SECONDS",
    "WARN_ITEM_BYTES",
    "CacheTier",
    "ConcurrentModification",
    "ConfigurationError",
    "ConfigurationRecord",
    "ConfigurationStore",
    "ConfigurationTableNotConfigured",
    "ConfigurationTooLarge",
    "InvalidProfileName",
    "ReservedProfileName",
    "ResolvedDocument",
    "ResolvedSection",
    "SectionSpec",
    "UnknownConfigurationSection",
    "deep_merge",
    "is_profile_key",
    "profile_key",
    "register",
    "registered_sections",
    "section_spec",
    "validate_profile_name",
]
