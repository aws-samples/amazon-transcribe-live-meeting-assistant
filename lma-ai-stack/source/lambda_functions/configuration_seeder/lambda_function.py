#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Seeds the configuration table when the stack is created or updated.

Three writes, each with different rules about what it may overwrite:

``Config#default``
    The deployment baseline, built from the JSON shipped in ``defaults/`` and
    from the values of the stack parameters that are the defaults of runtime
    settings (the ``ParameterDefaults`` property; see ``lma_config.parameters``).
    Written on create and rewritten on every update this function runs on,
    because it is this deployment's own content and a new release has to land.
    Which updates it runs on is decided by the content hash described at the
    end of this docstring and by those parameters' values, not by this
    function.

``Config#custom``
    The operator's profile. Written only on a table that has never been seeded
    (no ``Config#__active``): a new deployment, an upgrade from a release with
    no configuration store, or a table restored empty. The condition is on the
    records, not on the CloudFormation request type, which would miss the
    restored table. A new deployment's profile holds nothing, and
    ``settle_profiles`` removes it again so the deployment runs on its default;
    a deployment that has run without it is not given it back.

    How it came to exist is stamped on the record as ``ProfileOrigin`` and
    reported by every later run, which is what keeps a failed adoption visible
    after the deployment that caused it.

``Config#__active``
    The pointer naming the profile in force. Also written only if absent, so an
    operator who selects a different profile keeps their choice.

Then, after those writes (``settle_profiles``), the deployment is left running
as IDP's do: on the default profile, which every release rewrites, unless an
Admin changed something. A ``custom`` profile holding settings is stored as a
full copy and stays active; one holding none is deleted and the default is
activated. And each shipped preset the deployment can run is written as a
read-only Managed profile (``write_managed_profiles``).

Whenever ``Config#custom`` is absent and a deployment already has prompt
templates in the old per-store table, this adopts them into the profile so the
upgrade is not experienced as a reset. Absent rather than "on create" for the
same reason as above: a restored table or a hand-deleted record leaves the slot
empty on an update too. The adoption is the same conditional write, so it can
only ever fill an empty slot.

Stores that moved into the table after the profile could already exist are
listed in ``LEGACY_SECTIONS``. When this run creates the profile, their edits
go into it as it is created. Otherwise each is carried across one section at a
time: while the profile does not hold the section, its edits are read from the
previous table and added to the profile; once it holds the section -- an empty
one included -- nothing is read again. The outcome of each is reported as
``SectionAdoption``.

Given the revision bucket, it also makes sure the operator's profile has a
revision history. A profile this run creates starts at revision 1, holding
everything carried across; a profile that exists with no history yet has its
state as it stands recorded. The outcome is reported as ``RevisionHistory``.

CloudFormation re-invokes this on UPDATE only when one of the custom resource's
properties changes, so the template passes a hash of the shipped content *and*
of this handler. A property that never changes would mean a resource that is
never re-invoked, and a new release's prompts would never reach the table.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, NamedTuple, Optional, Tuple

import boto3
import cfnresponse  # vendored beside this handler; see cfnresponse.py
from botocore.exceptions import ClientError

# pylint: disable=import-error
from lma_config import (  # from the configuration layer
    ACTIVE_POINTER_KEY,
    ASR_ENGINE,
    CALL_PRICING,
    CHAT_BUTTONS,
    MEETING_CONSENT,
    NOVA_SONIC_CONFIG,
    RETENTION,
    SUMMARY_PROMPT_TEMPLATES,
    ConcurrentModification,
    ConfigurationStore,
    InvalidConfiguration,
    ProfileExists,
    RevisionHistoryUnavailable,
    SectionSpec,
    apply_parameter_defaults,
    apply_preset,
    codec,
    full_section,
    parameter_defaults,
    preset_by_id,
    preset_offered,
    registered_sections,
    shipped_presets,
    shipped_setting_defaults,
    validate_section,
)

# pylint: enable=import-error

# Stdlib logging rather than aws_lambda_powertools, which is the convention for
# the request-serving handlers: this function carries only the configuration
# layer, which is deliberately pure-Python and architecture-neutral so that it
# can be attached to every function that reads configuration whatever
# architecture it runs on. The other CloudFormation custom resources in this
# repository log the same way.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

#: Where the shipped baseline content lives inside this function's package.
DEFAULTS_DIR = Path(os.environ.get("LAMBDA_TASK_ROOT", Path(__file__).parent)) / "defaults"

#: Domain -> the file holding that domain's summary prompts. The healthcare set
#: is the alternative the llm-template-setup stack has always offered.
#:
#: These files are the authoritative copy: they are the ones read. The
#: llm-template-setup stack still ships its own copy because it still seeds the
#: table a rollback would read, so a prompt change has to be made here *and*
#: there until that stack is retired.
#: `test_the_shipped_prompts_match_the_copy_the_other_stack_still_ships` fails
#: if the two drift.
PROMPT_TEMPLATE_FILES = {
    "default": "LLMPromptSummaryTemplate.json",
    "healthcare": "LLMPromptHealthcareSummaryTemplate.json",
}

#: The shipped Nova Sonic voice-assistant settings. Like the prompt files above,
#: the nova-sonic-config stack still ships its own copy, because it still seeds
#: the table a rollback would read;
#: `test_the_shipped_nova_sonic_defaults_match_the_copy_the_other_stack_still_ships`
#: fails if the two drift.
NOVA_SONIC_DEFAULTS_FILE = "NovaSonicConfig.json"

#: The shipped Meeting Assist chat buttons, label -> prompt, numbered into
#: "N#Label" in the order they appear, exactly as the chat-button-config stack
#: numbers its own copy -- which it still ships, for the same reason as above;
#: `test_the_shipped_chat_buttons_match_the_copy_the_other_stack_still_ships`
#: fails if the two drift.
CHAT_BUTTONS_DEFAULTS_FILE = "ChatButtonConfig.json"

#: The price table for the per-meeting cost estimate. See
#: docs/meeting-cost-estimate.md for where each price came from.
CALL_PRICING_FILE = "CallPricing.json"
#: The on-demand ASR engine's switches as a new deployment ships them: all off,
#: so every meeting is transcribed by Amazon Transcribe until an administrator
#: chooses otherwise. The previous table had no seeder and read an absent record
#: as exactly this, so there is no second copy to keep in step.
ASR_ENGINE_DEFAULTS_FILE = "AsrEngine.json"
#: Meeting consent as a new deployment ships it: off, with the shipped notice
#: texts and phrases ready for an administrator who turns it on. The same values
#: as lma_consent.rules.DEFAULT_SETTINGS, which its tests hold to this file.
MEETING_CONSENT_DEFAULTS_FILE = "MeetingConsent.json"
# The Retention settings with no stack parameter. The two periods in days are
# added to the same section from their parameters (apply_parameter_defaults).
RETENTION_DEFAULTS_FILE = "Retention.json"

#: The profile an operator's edits land in. One profile in this stage; the key
#: layout holds as many as wanted.
OPERATOR_PROFILE = "custom"

#: The item the old per-store table holds an operator's edits in.
LEGACY_CUSTOM_KEY = "CustomSummaryPromptTemplates"
LEGACY_KEY_ATTRIBUTE = "LLMPromptTemplateId"

#: Internal marker on the adopted document saying the previous table could not
#: be read. Stripped before anything is stored; see `adopt_legacy_prompts`.
READ_FAILED_MARKER = "_read_failed"

#: Reported for a profile carrying no origin. Only a record created outside this
#: function can be in that state.
UNKNOWN_ORIGIN = "unknown"

DEFAULT_DESCRIPTION = (
    "Deployment baseline, rewritten by CloudFormation whenever a release changes "
    "the content it ships. "
    "Edits belong in a profile: anything written here is replaced by the next "
    "deployment."
)


def numbered_prompts(prompts: Mapping[str, str]) -> Dict[str, str]:
    """Prefix each prompt with its sequence number, as "N#Title".

    N orders the sections of the generated summary, and the reader selects
    prompts by this form, so the numbering is part of the stored shape rather
    than a display concern. Insertion order of the shipped JSON is the order.
    """
    return {f"{index}#{title}": prompt for index, (title, prompt) in enumerate(prompts.items(), 1)}


def shipped_default_body(domain: str) -> Dict[str, Any]:
    """The baseline document for ``domain``, read from the shipped JSON.

    The price table is the same whatever the domain. It is part of the baseline,
    so a release that updates a price reaches every deployment on its next stack
    update; a price an administrator changed lives in the profile and keeps
    winning over it.
    """
    filename = PROMPT_TEMPLATE_FILES.get(domain.lower(), PROMPT_TEMPLATE_FILES["default"])
    prompts = json.loads((DEFAULTS_DIR / filename).read_text(encoding="utf-8"))
    nova_sonic = json.loads((DEFAULTS_DIR / NOVA_SONIC_DEFAULTS_FILE).read_text(encoding="utf-8"))
    buttons = json.loads((DEFAULTS_DIR / CHAT_BUTTONS_DEFAULTS_FILE).read_text(encoding="utf-8"))
    pricing = json.loads((DEFAULTS_DIR / CALL_PRICING_FILE).read_text(encoding="utf-8"))
    asr_engine = json.loads((DEFAULTS_DIR / ASR_ENGINE_DEFAULTS_FILE).read_text(encoding="utf-8"))
    consent = json.loads((DEFAULTS_DIR / MEETING_CONSENT_DEFAULTS_FILE).read_text(encoding="utf-8"))
    retention = json.loads((DEFAULTS_DIR / RETENTION_DEFAULTS_FILE).read_text(encoding="utf-8"))
    body: Dict[str, Any] = {
        SUMMARY_PROMPT_TEMPLATES.name: numbered_prompts(prompts),
        NOVA_SONIC_CONFIG.name: nova_sonic,
        CHAT_BUTTONS.name: numbered_prompts(buttons),
        CALL_PRICING.name: pricing,
        ASR_ENGINE.name: asr_engine,
        MEETING_CONSENT.name: consent,
        RETENTION.name: retention,
    }
    # The settings that used to be stack parameters, at their shipped defaults
    # (lma_config/setting_defaults.json).
    for section, values in shipped_setting_defaults().items():
        body.setdefault(section, {}).update(values)
    # The two retention periods are still stack parameters: `seed` fills them
    # from the parameters' values. Present here, so the baseline holds every
    # section whatever the template passed.
    for entry in parameter_defaults():
        body.setdefault(entry.section, {})
    return body


def align_with_baseline(prompts: Mapping[str, str], baseline: Mapping[str, str]) -> Dict[str, str]:
    """The adopted prompts as a delta over the baseline: realigned, and pruned.

    Two things have to happen for an adopted record to be a delta, and the old
    page's behaviour is the reason for both.

    **Realigned.** It numbered sections by their position in its list, so a
    deployment that had removed or reordered a shipped section stored its
    remaining ones under numbers that no longer line up with the ones the
    release ships -- `1#DETAILS` where the baseline has `2#DETAILS`. Adopted
    verbatim those become additions rather than overrides: the merged document
    would hold both keys and the summary would have that section twice. (The
    previous summariser merged the two records the same way and produced the
    same duplicate; what is new is that the editor shows the merged document, so
    an operator would see it.) Matching on the section title fixes that; a title
    the release does not ship keeps its own number, because there is nothing to
    line it up with.

    **Pruned.** It also saved the *whole* list on every save, so a deployment
    whose administrator ever pressed Save arrives with every section stored --
    including the ones they never touched. Kept, those pin the prompts of the
    release they were saved under, and no later release's improvement to any of
    them would ever reach that deployment again. A prompt identical to the one
    the baseline now ships is therefore dropped: the merged document is the same
    either way, and the profile is a delta from the first deploy rather than
    from whenever somebody next opens the page.

    What is deliberately *not* dropped is a prompt that differs from the current
    baseline, even though a release having since changed that section means it
    may be the previous shipped text rather than anything anybody wrote. The two
    are indistinguishable from here, and keeping text an operator may have
    written is the safer error; the next save from the page prunes it if they
    did not want it.
    """
    by_title = {key.split("#", 1)[1]: key for key in baseline if "#" in key}
    aligned: Dict[str, str] = {}
    for key, value in sorted(prompts.items()):
        title = key.split("#", 1)[1]
        target = by_title.get(title, key)
        if baseline.get(target) == value:
            LOGGER.info(
                "not adopting %r: it is the prompt this release ships for %r, so the "
                "profile inherits it and a later release can still improve it",
                key,
                target,
            )
            continue
        if target in aligned:
            # The old page's positional renumbering could leave one title under
            # two numbers. Both cannot line up with the single key the baseline
            # ships, so one is dropped -- named here, because the operator whose
            # prompt it was is the only person who can tell which they wanted.
            LOGGER.warning(
                "two adopted prompts have the title %r; keeping the first by key "
                "and dropping %r. Check the Transcript Summary section of the Configuration page.",
                title,
                key,
            )
            continue
        aligned[target] = value
    return aligned


def adopt_legacy_prompts(
    table_name: str, baseline: Optional[Mapping[str, str]] = None, client: Any = None
) -> Dict[str, Any]:
    """The operator's edits from the old per-store table, if there are any.

    Returns an empty document when the table is not given, the record is absent,
    or it holds no prompts -- all three of which are the ordinary case for a new
    deployment.

    ``baseline`` is this release's prompt section. Adopted prompts are lined up
    against it by section title; see :func:`align_with_baseline` for why.

    A failure to read is logged and returns ``{"_read_failed": True}`` rather
    than raising: a fresh, empty profile is a worse upgrade than one carrying the
    old edits, but it is far better than a stack create that rolls back. The
    marker is what lets :func:`seed` record a distinct origin, stamped on the
    record as ``ProfileOrigin`` and published as the
    ``ConfigurationProfileOrigin`` stack output -- because this is a one-shot,
    the profile it creates being present is what stops any later deployment
    trying again, and an operator is the only person who can put it right.
    """
    if not table_name:
        return {}
    dynamodb = client if client is not None else boto3.client("dynamodb")
    try:
        response = dynamodb.get_item(
            TableName=table_name,
            Key={LEGACY_KEY_ATTRIBUTE: {"S": LEGACY_CUSTOM_KEY}},
            ConsistentRead=True,
        )
    except Exception:  # noqa: BLE001 -- see the docstring; never fail the deploy for this
        LOGGER.exception(
            "could not read the previous prompt-template table %s; starting with an empty profile",
            table_name,
        )
        return {READ_FAILED_MARKER: True}

    item = response.get("Item") or {}
    prompts = {
        name: value["S"]
        for name, value in item.items()
        if "#" in name and name.split("#", 1)[0].isdigit() and "S" in value
    }
    if not prompts:
        return {}
    aligned = align_with_baseline(prompts, baseline or {})
    if not aligned:
        # Everything the record held is what this release already ships, so
        # there is nothing to carry across. Reported as nothing rather than as
        # an empty section: `{section: {}}` is truthy, and `seed` would call
        # that "created-from-previous-table" when no prompt moved.
        LOGGER.info(
            "nothing to adopt from %s: every prompt it holds is one this release "
            "ships, so the profile inherits them all",
            table_name,
        )
        return {}
    LOGGER.info(
        "adopting %d prompt template(s) from %s into the configuration profile: %s, stored as %s",
        len(aligned),
        table_name,
        sorted(prompts),
        sorted(aligned),
    )
    return {SUMMARY_PROMPT_TEMPLATES.name: aligned}


class LegacySection(NamedTuple):
    """A section whose operator edits used to live in a table of their own.

    A NamedTuple rather than a dataclass: tests load this module by file path
    under a private name, and a dataclass resolves its annotations by looking
    its module up in `sys.modules`, where such a module is not.

    Attributes:
        section: The section the edits move into.
        property_name: The custom resource property naming the previous table.
        key_attribute: That table's partition-key attribute.
        key: The item holding the operator's edits.
        carries: Whether an attribute of that item is carried across. Everything
            else on the item -- its key, the seeded guidance text, the audit
            attributes -- is bookkeeping of the previous table rather than
            configuration.
    """

    section: SectionSpec
    property_name: str
    key_attribute: str
    key: str
    carries: Callable[[str], bool]


#: The Nova Sonic fields updateNovaSonicConfig saves, which are the ones its
#: resolver accepts.
#: `test_the_seeder_adopts_exactly_the_fields_this_resolver_accepts`, in the
#: resolver's own test module,
#: holds the two lists equal.
NOVA_SONIC_FIELDS = frozenset(
    {
        "systemPrompt",
        "promptMode",
        "modelId",
        "voiceId",
        "endpointingSensitivity",
        "groupMeetingMode",
        "meetingMode",
        "translatorLanguageA",
        "translatorLanguageB",
        "translatorMutePhrases",
        "translatorUnmutePhrases",
    }
)

#: The switches updateAsrConfig saves, which are the ones its resolver
#: accepts.
#: `test_the_seeder_adopts_exactly_the_switches_this_resolver_accepts` holds the
#: two lists equal.
ASR_ENGINE_FIELDS = frozenset(
    {"streamingEngineMicrovm", "virtualParticipantEngineMicrovm", "diarizeVirtualParticipant"}
)

#: A key in "N#Label" form, where N orders the entries. ASCII digits only.
SEQUENCED_KEY = re.compile(r"^[0-9]+#")

#: Stores moved in after the profile already existed, so their edits are carried
#: across one section at a time rather than by creating the profile.
LEGACY_SECTIONS = (
    LegacySection(
        section=NOVA_SONIC_CONFIG,
        property_name="LegacyNovaSonicConfigTableName",
        key_attribute="NovaSonicConfigId",
        key="CustomNovaSonicConfig",
        carries=NOVA_SONIC_FIELDS.__contains__,
    ),
    LegacySection(
        section=CHAT_BUTTONS,
        property_name="LegacyChatButtonConfigTableName",
        key_attribute="ChatButtonConfigId",
        key="CustomChatButtonConfig",
        # Every "N#Label" attribute, which is what the Edit Chat Buttons dialog
        # saves. Carried as the whole list it is: a non-empty custom list
        # replaces the shipped buttons rather than adding to them, so nothing
        # in it can be pruned against the baseline.
        carries=lambda name: bool(SEQUENCED_KEY.match(name)),
    ),
    LegacySection(
        section=ASR_ENGINE,
        property_name="LegacyAsrConfigTableName",
        key_attribute="AsrConfigId",
        key="CustomAsrConfig",
        # The three switches updateAsrConfig saves. A record
        # written by an older release can also hold retired tuning fields,
        # which no reader uses; they are left behind.
        carries=ASR_ENGINE_FIELDS.__contains__,
    ),
)

#: How many times a section adoption is attempted when an administrator's save
#: keeps landing between its read and its write. Three in a row during one
#: deployment is not a race worth handling further.
ADOPTION_ATTEMPTS = 3


def read_legacy_section(
    legacy: LegacySection, table_name: str, client: Any = None
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """The operator's edits for one section, from the table they used to live in.

    Returns ``(outcome, values)``. ``values`` is ``None`` when nothing should be
    written: no previous table was named, or it could not be read. In the
    second case the section stays absent from the profile, so the next run that
    re-seeds tries again -- which is safe because the section's presence, not
    this function, is what stops a later run replacing newer values.
    """
    if not table_name:
        return "no-previous-table", None
    dynamodb = client if client is not None else boto3.client("dynamodb")
    try:
        response = dynamodb.get_item(
            TableName=table_name,
            Key={legacy.key_attribute: {"S": legacy.key}},
            ConsistentRead=True,
        )
    except Exception:  # noqa: BLE001 -- never fail the deploy for this; see the docstring
        LOGGER.exception(
            "could not read %s from the previous table %s; the %s section is left "
            "unset and the next deployment that re-seeds tries again",
            legacy.key,
            table_name,
            legacy.section.name,
        )
        return "previous-table-unreadable", None
    item = codec.unmarshal_item(response.get("Item") or {})
    values = {name: value for name, value in item.items() if legacy.carries(name)}
    if not values:
        return "nothing-to-adopt", {}
    LOGGER.info(
        "adopting %s from %s into the %s section: %s",
        legacy.key,
        table_name,
        legacy.section.name,
        sorted(values),
    )
    return "adopted-from-previous-table", values


def adopt_legacy_section(
    store: ConfigurationStore, legacy: LegacySection, table_name: str, client: Any = None
) -> str:
    """Carry one section's edits into the operator's profile, once.

    The profile exists by the time this runs. Nothing is read from the previous
    table once the profile holds the section, whether this function put it there
    or an administrator saved it; an empty section counts.
    """
    for _attempt in range(ADOPTION_ATTEMPTS):
        profile = store.read_profile(OPERATOR_PROFILE, consistent_read=True)
        if not profile.exists or legacy.section.name in profile.body:
            return "already-present"
        outcome, values = read_legacy_section(legacy, table_name, client=client)
        if values is None:
            return outcome
        try:
            added = store.seed_section(OPERATOR_PROFILE, legacy.section.name, values)
        except ConcurrentModification:
            # Something saved the profile in between -- most likely an
            # administrator saving a different page. Read again and decide again.
            continue
        except RevisionHistoryUnavailable:
            # The profile's current state could not be recorded, so the store
            # will not replace it. Not a failed deployment: the section is left
            # unset, the shipped settings apply, and the next deployment that
            # runs the seeder tries again, as for an unreadable previous table.
            LOGGER.exception(
                "could not add the %s section: the profile's revision history could not "
                "be written; the next deployment that runs the seeder tries again",
                legacy.section.name,
            )
            return "revision-history-unavailable"
        return outcome if added else "already-present"
    raise ConcurrentModification(
        f"the configuration profile {OPERATOR_PROFILE!r} changed on each of "
        f"{ADOPTION_ATTEMPTS} attempts to add the {legacy.section.name} section"
    )


def custom_configuration(document: Any, source: str) -> Dict[str, Any]:
    """The sections of a custom configuration file, checked against their schemas.

    Either a plain JSON or YAML object of section name to values, or a
    Configuration page export (``"format": "lma-configuration-profile"``), whose
    ``sections`` are used.

    Raises:
        InvalidConfiguration: the file is not an object of known sections, or a
            section does not match its schema. The stack update then fails, with
            the problems in its status reason, rather than deploying a
            configuration nobody asked for.
    """
    if isinstance(document, dict) and document.get("format") == "lma-configuration-profile":
        document = document.get("sections")
    if not isinstance(document, dict):
        raise InvalidConfiguration([f"{source}: must hold an object of configuration sections"])
    known = registered_sections()
    problems = [f"{name}: is not a configuration section" for name in document if name not in known]
    for name, values in document.items():
        if name in known:
            try:
                validate_section(name, values)
            except InvalidConfiguration as error:
                problems.extend(error.problems)
    if problems:
        raise InvalidConfiguration(problems)
    return document


def read_custom_configuration(uri: str, *, client: Any = None) -> Dict[str, Any]:
    """Read and check the CustomConfigPath file: ``s3://bucket/key``, JSON or YAML."""
    match = re.fullmatch(r"s3://([^/]+)/(.+)", uri.strip())
    if not match:
        raise InvalidConfiguration([f"CustomConfigPath: {uri!r} is not an s3://bucket/key URI"])
    s3 = client or boto3.client("s3")
    raw = s3.get_object(Bucket=match.group(1), Key=match.group(2))["Body"].read().decode("utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        import yaml  # pylint: disable=import-outside-toplevel -- only for a YAML file

        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise InvalidConfiguration([f"{uri}: is neither JSON nor YAML: {error}"]) from error
    return custom_configuration(document, uri)


def write_managed_profiles(
    store: ConfigurationStore, baseline: Mapping[str, Any], settings: Mapping[str, Any]
) -> str:
    """Write each shipped preset this deployment offers as a Managed profile.

    The preset applied over the baseline. A preset needing something the
    deployment lacks (see ``preset_offered``) is not written, and a Managed
    profile for a preset no longer shipped or offered is retired unless it is
    active. Reported as ``id=outcome`` pairs.
    """
    outcomes: Dict[str, str] = {}
    written = set()
    for preset in shipped_presets():
        if not preset_offered(preset, dict(settings)):
            outcomes[preset["Id"]] = "not-offered"
            continue
        try:
            outcomes[preset["Id"]] = store.write_managed_profile(
                preset["Id"], apply_preset(dict(baseline), preset), description=preset["Title"]
            )
            written.add(preset["Id"])
        except ProfileExists:
            LOGGER.warning(
                "a Custom profile named %s exists; its Managed profile was not written",
                preset["Id"],
            )
            outcomes[preset["Id"]] = "name-in-use"
    for profile in store.list_profiles():
        if profile.managed and profile.name not in written and profile.name != "default":
            outcomes[profile.name] = (
                "retired" if store.retire_managed_profile(profile.name) else "kept-active"
            )
    return ",".join(f"{name}={outcome}" for name, outcome in sorted(outcomes.items()))


def seed(
    store: ConfigurationStore,
    *,
    domain: str,
    legacy_table_name: str = "",
    legacy_client: Any = None,
    legacy_section_tables: Optional[Mapping[str, str]] = None,
    parameter_values: Optional[Mapping[str, Any]] = None,
    configuration_preset: str = "",
    custom_sections: Optional[Mapping[str, Any]] = None,
    deployment_settings: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Apply the writes and report what each one did.

    ``legacy_section_tables`` maps a section name in :data:`LEGACY_SECTIONS` to
    the previous table holding its edits; ``legacy_client`` reads those as well
    as the prompt-template table. ``parameter_values`` is the
    ``ParameterDefaults`` property: stack parameter name to value, for the
    parameters that are the defaults of runtime settings (see
    ``lma_config.parameters``).
    """
    baseline = shipped_default_body(domain)
    # The values of the stack parameters that are the defaults of runtime
    # settings. They belong in the baseline, not the profile: the baseline is
    # what the deployment was deployed with, so a stack update that changes a
    # parameter reaches every setting an administrator has not changed, while
    # one they have changed, which lives in the profile, keeps winning.
    applied = apply_parameter_defaults(baseline, parameter_values or {})
    # Then the ConfigurationPreset stack parameter's preset, and the
    # CustomConfigPath file over that, as IDP's deployments choose their
    # default configuration. Both are part of what the deployment was deployed
    # with, so both belong in the baseline.
    preset_applied = "none"
    if configuration_preset and configuration_preset != "none":
        chosen = preset_by_id(configuration_preset)
        if not preset_offered(chosen, dict(deployment_settings or {})):
            raise InvalidConfiguration(
                [
                    f"ConfigurationPreset: {configuration_preset} sets something this deployment "
                    "does not include (the on-demand ASR preset needs EnableMicrovmAsr)"
                ]
            )
        baseline = apply_preset(baseline, chosen)
        preset_applied = configuration_preset
    if custom_sections:
        for name, values in custom_sections.items():
            baseline[name] = full_section(name, values, baseline.get(name))
    store.write_default(baseline, description=DEFAULT_DESCRIPTION)

    # The guard that actually protects an operator's edits is the condition on
    # the write inside `seed_profile`; this read only decides which outcome to
    # report and saves a pointless read of the previous table on every
    # deployment that already has a profile.
    existing = store.read_profile(OPERATOR_PROFILE, consistent_read=True)
    tables = legacy_section_tables or {}
    # The later stores' outcomes, when this run creates the profile with their
    # sections already in it; see below.
    carried: Dict[str, str] = {}
    # The operator's profile is created only on a table that has never been
    # seeded -- a new deployment, or an upgrade from a release with no
    # configuration store -- which is when there is anything to carry into it.
    # A deployment that has run without it since, because it was removed as
    # empty or an Admin deleted it, is not given it back.
    first_seeding = not store.read_record(ACTIVE_POINTER_KEY, consistent_read=True).exists
    if not existing.exists and not first_seeding:
        profile_action = "not-needed"
        profile_origin = UNKNOWN_ORIGIN
    elif existing.exists:
        profile_action = "kept"
        # Read back rather than recomputed. This run cannot tell what an upgrade
        # did months ago, and the one thing somebody may still have to act on --
        # that the adoption failed -- has to survive every later run: the content
        # hash makes the seeder run again on any release that changes either of
        # its directories, and every one of those finds the profile present.
        profile_origin = existing.profile_origin or UNKNOWN_ORIGIN
    else:
        adopted = adopt_legacy_prompts(
            legacy_table_name,
            baseline.get(SUMMARY_PROMPT_TEMPLATES.name, {}),
            client=legacy_client,
        )
        read_failed = adopted.pop(READ_FAILED_MARKER, False)
        if read_failed:
            # The one outcome an operator has to act on: the profile now exists,
            # so no later deployment retries the adoption, and prompts edited
            # before the upgrade have to be applied again from the Transcript
            # Summary page.
            profile_origin = "created-previous-table-unreadable"
        elif adopted:
            profile_origin = "created-from-previous-table"
        else:
            profile_origin = "created"
        # The later stores' sections go into the profile as it is created,
        # rather than being added one write at a time after it. The result is
        # the same profile, but recorded as one revision: a new deployment, and
        # an upgrade from a release with no configuration store, start their
        # history with revision 1 holding the whole configuration as it arrived,
        # not with one near-empty revision per section.
        sections = {
            legacy.section.name: read_legacy_section(
                legacy, tables.get(legacy.section.name, ""), client=legacy_client
            )
            for legacy in LEGACY_SECTIONS
        }
        body = dict(adopted)
        for name, (_outcome, values) in sections.items():
            if values is not None:
                body[name] = values
        created = store.seed_profile(OPERATOR_PROFILE, body, origin=profile_origin)
        if created:
            profile_action = "created"
            carried = {name: outcome for name, (outcome, _values) in sections.items()}
        else:
            # Lost the race with something else creating it. Whatever that was
            # recorded is the truth; this run's reading of it is not.
            profile_action = "kept"
            profile_origin = (
                store.read_profile(OPERATOR_PROFILE, consistent_read=True).profile_origin
                or UNKNOWN_ORIGIN
            )

    # Before the section adoption below, so that a profile whose history has not
    # started has its state as it stood recorded first, and anything adopted is
    # recorded after it.
    revision_history = start_revision_history(store)

    adoption = {
        legacy.section.name: carried.get(legacy.section.name)
        or adopt_legacy_section(
            store, legacy, tables.get(legacy.section.name, ""), client=legacy_client
        )
        for legacy in LEGACY_SECTIONS
    }

    pointer_outcome = (
        "created" if store.set_active_profile(OPERATOR_PROFILE, only_if_absent=True) else "kept"
    )
    return {
        # One "section=outcome" pair per store moved in after the profile
        # existed, published as the ConfigurationSectionAdoption stack output.
        # Unlike ProfileOrigin this is what *this* run found, and that is
        # enough: an adoption that could not read the previous table leaves the
        # section unset, so every later run reports it again until it lands.
        "SectionAdoption": ",".join(f"{name}={outcome}" for name, outcome in adoption.items()),
        "Baseline": "written",
        # The preset the baseline was built from (ConfigurationPreset), and
        # whether a CustomConfigPath file was laid over it.
        "ConfigurationPreset": preset_applied,
        "CustomConfiguration": "applied" if custom_sections else "none",
        # Which stack parameters the baseline carries as the defaults of runtime
        # settings. One missing here is one whose readers fall back to their
        # environment variable.
        "ParameterDefaults": ",".join(applied) or "none",
        # What this run did, which is almost always "kept".
        "Profile": profile_action,
        # How the profile came to exist, whenever that was. This is the value
        # the stack publishes, because it is the one that stays true.
        "ProfileOrigin": profile_origin,
        "ActiveProfile": pointer_outcome,
        # What this run found of the profile's revision history, as
        # "custom=outcome"; published as the ConfigurationRevisionHistory output.
        "RevisionHistory": f"{OPERATOR_PROFILE}={revision_history}",
    }


def start_revision_history(store: ConfigurationStore) -> str:
    """Make the operator's profile, as it stands, a recorded revision.

    On the deployment that introduces revision history this records the profile
    as revision 1; every later run finds it recorded and reports ``present``.
    See ``ConfigurationStore.ensure_revision_history`` for the other outcomes.

    Never fails the deployment. If the history cannot be written now, the
    outcome says so, and nothing is lost by waiting: the first save from a
    configuration page records the profile before replacing it, and is refused
    rather than made if it cannot.
    """
    if not store.keeps_history:
        return "not-configured"
    try:
        return store.ensure_revision_history(OPERATOR_PROFILE)
    except Exception:  # noqa: BLE001 -- see the docstring; never fail the deploy for this
        LOGGER.exception(
            "could not start the revision history of the %s profile; the first save "
            "records it instead",
            OPERATOR_PROFILE,
        )
        return "not-started"


#: Section adoption outcomes that leave a section unset on purpose, so that the
#: next deployment retries. Converting the profile meanwhile would fill the
#: section from the baseline and stop the retry.
PENDING_ADOPTION = frozenset({"previous-table-unreadable", "revision-history-unavailable"})


def _holds_settings(body: Mapping[str, Any]) -> bool:
    """Whether a delta profile changes anything: a section with no values changes nothing."""
    return any(values for values in body.values())


def _adoption_outcomes(outcome: Mapping[str, str]) -> Dict[str, str]:
    """The ``SectionAdoption`` pairs of a :func:`seed` outcome, by section."""
    pairs = (outcome.get("SectionAdoption") or "").split(",")
    return dict(pair.split("=", 1) for pair in pairs if "=" in pair)


def settle_profiles(store: ConfigurationStore, adoption: Mapping[str, str]) -> str:
    """Leave the deployment running as IDP's do: on the default profile unless an Admin changed it.

    After :func:`seed`. The operator's ``custom`` profile, which an earlier
    release created on every deployment as a delta over the baseline:

    - holding settings (an Admin's edits, or ones adopted from a previous
      table) -- stored as a full copy, as every Custom profile is now, and left
      active if it was. The delta stays in its history as the revision before.
      Reported as "converted", then "full" on later runs.
    - holding none -- deleted, and the deployment default activated if it was
      the active profile, so the deployment follows each release's defaults
      until an Admin creates a Custom profile. Reported as "removed-empty".
    - absent -- the active-profile pointer is set to the default if it is
      unset. Reported as "absent".

    "deferred" when this cannot be done now: a section adoption is to be
    retried by the next deployment, the profile's current state could not be
    recorded in its history, or an Admin saved it meanwhile. Readers keep using
    the delta, and the next deployment tries again.
    """
    current = store.read_profile(OPERATOR_PROFILE, consistent_read=True)
    if not current.exists:
        store.set_active_profile("default", only_if_absent=True)
        return "absent"
    if current.metadata.get("ProfileFormat") == "full":
        return "full"
    if any(outcome in PENDING_ADOPTION for outcome in adoption.values()):
        return "deferred"
    try:
        if not _holds_settings(current.body):
            was_active = store.active_profile_name(consistent_read=True) == OPERATOR_PROFILE
            if was_active:
                store.activate_profile("default")
            try:
                store.delete_profile(OPERATOR_PROFILE)
            except ConcurrentModification:
                # An Admin saved into it meanwhile: it is theirs now, so it goes
                # back in force, and the next deployment converts it.
                if was_active:
                    store.activate_profile(OPERATOR_PROFILE)
                raise
            return "removed-empty"
        converted = store.convert_to_full_copy(
            OPERATOR_PROFILE, comment="stored as a full copy of the configuration"
        )
    except (RevisionHistoryUnavailable, ConcurrentModification, ClientError):
        LOGGER.warning(
            "could not settle the %s profile now; the next deployment retries",
            OPERATOR_PROFILE,
            exc_info=True,
        )
        return "deferred"
    return "converted" if converted else "full"


def handler(event: Dict[str, Any], context: Any) -> None:
    """CloudFormation custom resource entry point."""
    LOGGER.info("seeding the configuration table: %s", json.dumps(event, default=str))
    request_type = event.get("RequestType")
    properties = event.get("ResourceProperties") or {}
    # A physical id that does not change between invocations, so a new content
    # hash is an update to this resource rather than a replacement followed by a
    # Delete of the old one. cfnresponse falls back to the log stream name,
    # which differs every time -- including on the failure path below, where
    # answering with a different id than the resource already has would tell
    # CloudFormation the resource had been replaced by the attempt that failed.
    physical_id = f"configuration-seed/{properties.get('ConfigurationTableName', 'unknown')}"

    try:
        if request_type == "Delete":
            # The table's own DeletionPolicy decides whether the data survives;
            # emptying it here would make a stack update that replaced this
            # resource destroy configuration it had just written.
            cfnresponse.send(
                event, context, cfnresponse.SUCCESS, {"Deleted": "nothing"}, physical_id
            )
            return

        store = ConfigurationStore(
            properties["ConfigurationTableName"],
            # A property rather than an environment variable, like the table
            # name, so the resource's own properties say what it writes.
            revision_bucket=properties.get("ConfigurationBucketName", ""),
        )
        outcome = seed(
            store,
            domain=properties.get("Domain", "default"),
            legacy_table_name=properties.get("LegacyPromptTemplateTableName", ""),
            legacy_section_tables={
                legacy.section.name: properties.get(legacy.property_name, "")
                for legacy in LEGACY_SECTIONS
            },
            parameter_values=properties.get("ParameterDefaults") or {},
            configuration_preset=properties.get("ConfigurationPreset", ""),
            deployment_settings=properties.get("DeploymentSettings") or {},
            custom_sections=(
                read_custom_configuration(properties["CustomConfigPath"])
                if properties.get("CustomConfigPath")
                else None
            ),
        )
        outcome["ProfileFormat"] = settle_profiles(store, _adoption_outcomes(outcome))
        try:
            outcome["ManagedProfiles"] = write_managed_profiles(
                store,
                store.read_profile("default", consistent_read=True).body,
                properties.get("DeploymentSettings") or {},
            )
        except (RevisionHistoryUnavailable, ConcurrentModification, ClientError):
            # Like the conversion: the deployment goes ahead, and the next one
            # writes them. Nothing reads a Managed profile until it is activated.
            LOGGER.warning("could not write the Managed profiles now", exc_info=True)
            outcome["ManagedProfiles"] = "deferred"
        LOGGER.info("seeded the configuration table: %s", outcome)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, outcome, physical_id)
    except Exception as error:  # noqa: BLE001 -- must answer CloudFormation either way
        LOGGER.exception("could not seed the configuration table")
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical_id, reason=str(error))
