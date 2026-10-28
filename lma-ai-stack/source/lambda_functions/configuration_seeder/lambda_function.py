#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Seeds the configuration table when the stack is created or updated.

Three writes, each with different rules about what it may overwrite:

``Config#default``
    The deployment baseline, built from the JSON shipped in ``defaults/``.
    Written on create and rewritten on every update this function runs on,
    because it is this deployment's own content and a new release has to land.
    Which updates it runs on is decided by the content hash described at the
    end of this docstring, not by this function.

``Config#custom``
    The operator's profile. Written **only if it is not there**, so a stack
    update never discards an edit made from the web UI. The condition lives in
    the store's ``seed_profile``, not in a branch on the CloudFormation request
    type: conditioning on ``RequestType == 'Create'`` would also skip the case
    where the record is genuinely absent on an update, which is what happens
    after the table is restored or the record is deleted by hand.

    How it came to exist is stamped on the record as ``ProfileOrigin`` and
    reported by every later run, which is what keeps a failed adoption visible
    after the deployment that caused it.

``Config#__active``
    The pointer naming the profile in force. Also written only if absent, so an
    operator who selects a different profile keeps their choice.

Whenever ``Config#custom`` is absent and a deployment already has prompt
templates in the old per-store table, this adopts them into the profile so the
upgrade is not experienced as a reset. Absent rather than "on create" for the
same reason as above: a restored table or a hand-deleted record leaves the slot
empty on an update too. The adoption is the same conditional write, so it can
only ever fill an empty slot.

Stores that move into the table after the profile exists cannot use that
route, because the profile is already there. Each is listed in
``LEGACY_SECTIONS`` and carried across one section at a time: while the profile
does not hold the section, its edits are read from the previous table and added
to the profile; once it holds the section -- an empty one included -- nothing is
read again. The outcome of each is reported as ``SectionAdoption``.

CloudFormation re-invokes this on UPDATE only when one of the custom resource's
properties changes, so the template passes a hash of the shipped content *and*
of this handler. A property that never changes would mean a resource that is
never re-invoked, and a new release's prompts would never reach the table.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, NamedTuple, Optional, Tuple

import boto3
import cfnresponse  # vendored beside this handler; see cfnresponse.py

# pylint: disable=import-error
from lma_config import (  # from the configuration layer
    NOVA_SONIC_CONFIG,
    SUMMARY_PROMPT_TEMPLATES,
    ConcurrentModification,
    ConfigurationStore,
    SectionSpec,
    codec,
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
    """The baseline document for ``domain``, read from the shipped JSON."""
    filename = PROMPT_TEMPLATE_FILES.get(domain.lower(), PROMPT_TEMPLATE_FILES["default"])
    prompts = json.loads((DEFAULTS_DIR / filename).read_text(encoding="utf-8"))
    nova_sonic = json.loads((DEFAULTS_DIR / NOVA_SONIC_DEFAULTS_FILE).read_text(encoding="utf-8"))
    return {
        SUMMARY_PROMPT_TEMPLATES.name: numbered_prompts(prompts),
        NOVA_SONIC_CONFIG.name: nova_sonic,
    }


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
                "and dropping %r. Check the Transcript Summary page.",
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
        fields: The attributes carried across. Everything else on the item --
            its key, the seeded guidance text, the audit attributes -- is
            bookkeeping of the previous table rather than configuration.
    """

    section: SectionSpec
    property_name: str
    key_attribute: str
    key: str
    fields: FrozenSet[str]


#: The fields the Nova Sonic Configuration page saves, which are the ones the
#: resolver behind it accepts.
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

#: Stores moved in after the profile already existed, so their edits are carried
#: across one section at a time rather than by creating the profile.
LEGACY_SECTIONS = (
    LegacySection(
        section=NOVA_SONIC_CONFIG,
        property_name="LegacyNovaSonicConfigTableName",
        key_attribute="NovaSonicConfigId",
        key="CustomNovaSonicConfig",
        fields=NOVA_SONIC_FIELDS,
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
    values = {name: value for name, value in item.items() if name in legacy.fields}
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
        return outcome if added else "already-present"
    raise ConcurrentModification(
        f"the configuration profile {OPERATOR_PROFILE!r} changed on each of "
        f"{ADOPTION_ATTEMPTS} attempts to add the {legacy.section.name} section"
    )


def seed(
    store: ConfigurationStore,
    *,
    domain: str,
    legacy_table_name: str = "",
    legacy_client: Any = None,
    legacy_section_tables: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Apply the writes and report what each one did.

    ``legacy_section_tables`` maps a section name in :data:`LEGACY_SECTIONS` to
    the previous table holding its edits; ``legacy_client`` reads those as well
    as the prompt-template table.
    """
    baseline = shipped_default_body(domain)
    store.write_default(baseline, description=DEFAULT_DESCRIPTION)

    # The guard that actually protects an operator's edits is the condition on
    # the write inside `seed_profile`; this read only decides which outcome to
    # report and saves a pointless read of the previous table on every
    # deployment that already has a profile.
    existing = store.read_profile(OPERATOR_PROFILE, consistent_read=True)
    if existing.exists:
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
        created = store.seed_profile(OPERATOR_PROFILE, adopted, origin=profile_origin)
        if created:
            profile_action = "created"
        else:
            # Lost the race with something else creating it. Whatever that was
            # recorded is the truth; this run's reading of it is not.
            profile_action = "kept"
            profile_origin = (
                store.read_profile(OPERATOR_PROFILE, consistent_read=True).profile_origin
                or UNKNOWN_ORIGIN
            )

    tables = legacy_section_tables or {}
    adoption = {
        legacy.section.name: adopt_legacy_section(
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
        # What this run did, which is almost always "kept".
        "Profile": profile_action,
        # How the profile came to exist, whenever that was. This is the value
        # the stack publishes, because it is the one that stays true.
        "ProfileOrigin": profile_origin,
        "ActiveProfile": pointer_outcome,
    }


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

        store = ConfigurationStore(properties["ConfigurationTableName"])
        outcome = seed(
            store,
            domain=properties.get("Domain", "default"),
            legacy_table_name=properties.get("LegacyPromptTemplateTableName", ""),
            legacy_section_tables={
                legacy.section.name: properties.get(legacy.property_name, "")
                for legacy in LEGACY_SECTIONS
            },
        )
        LOGGER.info("seeded the configuration table: %s", outcome)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, outcome, physical_id)
    except Exception as error:  # noqa: BLE001 -- must answer CloudFormation either way
        LOGGER.exception("could not seed the configuration table")
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical_id, reason=str(error))
