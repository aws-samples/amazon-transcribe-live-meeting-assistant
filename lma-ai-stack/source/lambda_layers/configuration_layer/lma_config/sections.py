#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Configuration sections, and the caching policy each one declares.

LMA's existing runtime-configuration readers do not agree on how long a value
may be cached, and two of them disagree *deliberately*: the transcriber's ASR
switches are cached for 30 seconds so a burst of meetings starting together does
not each do its own read, while the voice-enrollment reader never caches at all,
because a withdrawal of consent is promised to take effect for every later
meeting and a cache is exactly a window in which it would not.

Both of those are right for what they read, which is why there is no single
policy here. A section declares its tier, and the tier decides the TTL and
whether the read is a consistent one. There is no way to ask for a section
without going through its declaration: :func:`section_spec` raises on a name
that is not registered rather than guessing, and no read API takes a TTL
argument.

====================== ==== ================= ==================================
Tier                   TTL  Read              For
====================== ==== ================= ==================================
``IMMEDIATE``          none ``ConsistentRead`` Anything the UI promises takes
                                              effect at once, and anything
                                              consent- or privacy-bearing.
``SHORT``              30 s eventual          Per-meeting switches read once at
                                              session start.
``STANDARD``           300s eventual          Prompts, model ids, numeric
                                              limits. Carries a fingerprint so
                                              a caller can tell a refetch that
                                              changed nothing from one that did.
====================== ==== ================= ==================================
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict

from .errors import UnknownConfigurationSection


class CacheTier(str, Enum):
    """How long a section's values may be reused, and how they are read."""

    IMMEDIATE = "immediate"
    SHORT = "short"
    STANDARD = "standard"


#: Seconds a resolved document may be reused when serving each tier. Zero is not
#: "a very short cache": the store treats it as "never serve from the cache".
TIER_TTL_SECONDS: Dict[CacheTier, float] = {
    CacheTier.IMMEDIATE: 0.0,
    CacheTier.SHORT: 30.0,
    CacheTier.STANDARD: 300.0,
}

#: Whether a refetch for this tier asks DynamoDB for a strongly consistent read.
#: Only the immediate tier does: for the other two the TTL already exceeds
#: DynamoDB's replication lag by orders of magnitude, so paying for consistency
#: would buy nothing.
TIER_CONSISTENT_READ: Dict[CacheTier, bool] = {
    CacheTier.IMMEDIATE: True,
    CacheTier.SHORT: False,
    CacheTier.STANDARD: False,
}


@dataclass(frozen=True)
class SectionSpec:
    """One section of the configuration document.

    Attributes:
        name: The key the section occupies in the configuration body.
        cache_tier: The declared caching policy. Part of the declaration rather
            than a call-site argument, so the decision is made once.
        description: What the section holds, for the registry listing and the
            documentation generated from it.
    """

    name: str
    cache_tier: CacheTier
    description: str


_REGISTRY: Dict[str, SectionSpec] = {}


def register(spec: SectionSpec) -> SectionSpec:
    """Add a section to the registry and return it.

    Raises:
        ValueError: a different section is already registered under the name.
            Two declarations of one section would mean two caching policies for
            it, and which one applied would depend on import order.
    """
    existing = _REGISTRY.get(spec.name)
    if existing is not None and existing != spec:
        raise ValueError(
            f"the configuration section {spec.name!r} is already registered as "
            f"{existing!r}, which declares a different caching policy"
        )
    _REGISTRY[spec.name] = spec
    return spec


def section_spec(name: str) -> SectionSpec:
    """The declaration for ``name``.

    Raises:
        UnknownConfigurationSection: nothing registered that name, so no
            caching policy is declared for it.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownConfigurationSection(
            f"no configuration section named {name!r} is registered; register it "
            f"with a declared cache tier. Registered: {sorted(_REGISTRY)}"
        ) from None


def registered_sections() -> Dict[str, SectionSpec]:
    """A copy of the registry, for documentation and tests."""
    return dict(_REGISTRY)


# ── the sections this stage populates ───────────────────────────────────────

#: The summary prompt templates: one prompt per section of a meeting summary,
#: keyed "N#Title" where N orders the sections. Read by the meeting-summary
#: Lambda once per summary and edited from the Transcript Summary page.
#:
#: Standard tier, because an end-of-meeting summary runs unattended and a prompt
#: edit reaching the next one a few minutes later is what the page says happens.
#: The one case that cannot wait is the Regenerate button, which somebody pressed
#: on purpose: its invoke carries `RefreshConfiguration` and the summariser drops
#: its cached copy before reading.
SUMMARY_PROMPT_TEMPLATES = register(
    SectionSpec(
        name="summary_prompt_templates",
        cache_tier=CacheTier.STANDARD,
        description=(
            "Prompts used to generate each section of a meeting summary, keyed "
            "'N#Title' where N orders the sections."
        ),
    )
)

#: The voice assistant's settings: the Nova Sonic system prompt and how it
#: combines with the shipped one, the model and voice, turn-taking sensitivity,
#: the meeting mode, and the translator languages and trigger phrases. Read by
#: the Virtual Participant when its voice assistant starts -- the ElevenLabs
#: provider reads the meeting-mode and translator fields from it too -- and
#: edited from the Nova Sonic Configuration page.
#:
#: Immediate tier. The reader runs once per meeting, so a cache would save at
#: most a read or two per meeting, and the page says a saved change applies to
#: the next meeting to start; a cache is a window in which it would not.
NOVA_SONIC_CONFIG = register(
    SectionSpec(
        name="nova_sonic_config",
        cache_tier=CacheTier.IMMEDIATE,
        description=(
            "Voice assistant settings: the Nova Sonic system prompt and prompt "
            "mode, model, voice, endpointing sensitivity, meeting mode, and the "
            "translator languages and trigger phrases."
        ),
    )
)

#: The Meeting Assist chat panel's quick-action buttons, keyed "N#Label" where N
#: orders them, each holding the prompt the button sends. Unlike the other
#: sections a profile's buttons do not override the shipped ones key by key: a
#: non-empty list in the profile is the whole list, and the shipped buttons
#: apply only while it is empty. So readers take the two records separately --
#: the chat panel reads both through the getChatButtonConfig resolver and picks
#: one -- rather than reading the merged section.
#:
#: Immediate tier. The resolver reads each record directly with a consistent
#: read, and the Edit Chat Buttons dialog shows the saved buttons straight
#: after a save; a reader of the section would have to honour the same promise.
CHAT_BUTTONS = register(
    SectionSpec(
        name="chat_buttons",
        cache_tier=CacheTier.IMMEDIATE,
        description=(
            "Meeting Assist chat panel quick-action buttons, keyed 'N#Label' where "
            "N orders them; a non-empty profile list replaces the shipped one."
        ),
    )
)

#: The price table the per-meeting cost estimate is computed with: US dollars per
#: unit for each metered service, a base region and per-region differences. Read
#: by the cost resolver whenever a meeting's estimate is drawn, and edited by an
#: administrator through the pricing API. See docs/meeting-cost-estimate.md.
#:
#: Standard tier: an estimate drawn a few minutes after a price edit using the
#: previous price is what the documentation says happens, and nothing about a
#: price is consent- or privacy-bearing. getCallPricing reads the stored records
#: directly, so an administrator always sees what they saved.
CALL_PRICING = register(
    SectionSpec(
        name="call_pricing",
        cache_tier=CacheTier.STANDARD,
        description=(
            "Unit prices for the per-meeting cost estimate: USD per token, second or "
            "vCPU-second for each metered service, by region."
        ),
    )
)
