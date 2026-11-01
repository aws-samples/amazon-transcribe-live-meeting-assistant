---
title: "Configuration Guide"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Configuration Guide

This page is for administrators. It explains which of LMA's settings you can
change from the web UI while the stack is running, how to change them on the
**Configuration** page, what happens when you save, and what to do when a save is
refused. How the settings are stored, cached and versioned underneath is
described in the [Unified Configuration Store](configuration-store.md) reference.

## What you can change without redeploying

Two kinds of setting control LMA. **Stack parameters** are chosen when the
CloudFormation stack is deployed or updated; they decide which resources exist,
such as whether the Virtual Participant or the on-demand speech engine is
deployed at all, and changing one means a stack update. They are listed in
[CloudFormation Parameters](cloudformation-parameters.md). **Runtime settings**
are what this page is about: they are stored in the stack's configuration table,
an Admin changes them from the web UI, and the change takes effect without a
deployment.

The runtime settings are grouped into five sections. Each section has a release
default, which ships with LMA and is updated by later releases, and your changes
over it. Only what you change is stored, so a setting you never touched keeps
following the release.

| Section | What it controls | A saved change applies |
|---|---|---|
| Transcript Summary | The sections of the end-of-meeting summary and the prompt behind each | To summaries generated within about five minutes of the save. **Regenerate** on a meeting page uses it at once |
| Meeting Assist Chat Buttons | The quick-action buttons in the chat panel. A saved list replaces the shipped buttons as a whole | At once, to chat panels opened after the save |
| Nova Sonic Voice Assistant | The voice assistant's system prompt and prompt mode, model, voice, turn-taking sensitivity, meeting mode and translator settings | To the next meeting the voice assistant joins |
| Transcription Engine (experimental) | Which engine transcribes streaming meetings and Virtual Participants, and Virtual Participant voice separation. Shown only when the on-demand speech engine is deployed | To meetings that start within 30 seconds of the save or later |
| Meeting Cost Prices | The unit prices the per-meeting [cost estimate](meeting-cost-estimate.md) is computed with | To estimates drawn within about five minutes of the save |

Every section is on **Configuration ▸ Configuration**. The older pages for single
sections — **Transcript Summary**, **Nova Sonic Config**, **Transcription
Engine**, and **Edit Chat Buttons** in a meeting's chat panel — still work and
save into the same configuration.

Only members of the **Admin** Cognito group can change any of these. Add someone
to the group on [User Management](user-management.md) if they need to.

## Using the Configuration page

Open **Configuration ▸ Configuration** in the navigation. Each section is a panel
that says what it controls and when a saved change applies; open one to edit it.
A link to `#/configuration/settings?section=call_pricing` (or any other section's
name) opens that section directly.

**Seeing what you changed.** A value that differs from the release default is
marked **Changed**, or **Added** for an entry the release does not ship, with a
coloured rule beside it, and a section's heading counts how many of its settings
differ. A section with edits you have not saved yet is marked **Unsaved
changes**.

**Putting a default back.** **Reset to default**, beside a changed value, puts
back the shipped value of that one setting. **Reset section to defaults** does it
for a whole section, and **Undo changes** discards what you have edited in a
section since the page loaded. **Discard changes**, at the top, does that for every
section.

**Lists of entries.** The summary sections and the chat buttons are lists you can
add to. A summary section the release ships cannot be deleted, because the next
release would bring it back; **Turn off** it instead, and the summary skips it.
**Turn on** brings back that one section with the shipped prompt. Renaming a
shipped section turns off the old title and adds the new one in the same place.
A chat button list is saved whole: you can remove any button and reorder the list,
and while you have saved no list of your own, the shipped buttons apply.

**Prices.** Prices are listed by region — `default` applies everywhere — then by
service, then by unit. You can change a shipped price, or add a region, service
or unit the table does not list. A shipped entry can be reset but not removed.

**Mistakes are caught as you type.** A value a save would refuse — a negative
price, an empty voice id, a region name that is not a region code — is marked
in red with what is wrong, and the page does not offer to save until it is
corrected.

## What happens when you save

Select **Save changes**. The dialog lists the sections you changed and asks for
two optional things:

- a **comment**, recorded with the save, so the history says why it was made;
- a **label**, which keeps this revision in the history however many saves
  follow it.

**The save is checked before anything is stored.** Every section has a schema
that says which settings it has and what values each accepts. The page checks
your changes against it as you edit, and the server checks again: a save that
breaks a rule is refused as a whole, nothing is stored, and the error names each
setting that was wrong. For example:

```
call_pricing › regions › default › transcribe/streaming › seconds: must be at least 0; got -1
```

Read the path from left to right: the section, then each key down to the setting.
A refusal can name a setting you did not touch: what an upgrade carried across
from an earlier release, and what a restore put back, are stored without this
check, and a later save of that section checks them. Correct the setting it
names, then save again.

**The save is checked against the version you loaded.** All the sections are
stored together, and a save is refused if anyone saved any of them after your
page loaded — on this page or one of the older ones — rather than silently
replacing their change. The page then offers two ways on:

- **Reload and keep my changes** loads what they saved and applies your changes
  over it. Settings you both changed now hold your value, and the page lists
  them so you can check them. Nothing is saved until you select **Save changes**
  again.
- **Discard my changes and reload** shows the configuration as it is now saved.

**Every save is recorded.** A save that changes something becomes a numbered
revision recording who saved it, when, which sections it changed, and your comment
and label. **View history** opens **Configuration ▸ Configuration History**,
where you can see the revisions, compare any two of them setting by setting,
label or relabel one, or restore an earlier one. A restore puts back every section
as it was in that revision and is itself recorded as a new revision, so it can be
undone the same way. See
[Revisions and audit](configuration-store.md#revisions-and-audit).

## Changing a setting outside the web UI

The page saves through the `updateConfiguration` GraphQL operation, which an
Admin can also call directly, with the same checks. It and the older per-section
operations are described in the
[Unified Configuration Store](configuration-store.md) reference. Editing the
configuration table in the DynamoDB console does not work: the records are
stored compressed, and an attribute added beside a compressed record is ignored.

## Related

- [Unified Configuration Store](configuration-store.md) — the storage, caching,
  validation and revision history behind these settings
- [Transcript Summarization](transcript-summarization.md),
  [Nova Sonic 2 Setup](nova-sonic-setup.md), [MicroVM ASR](microvm-asr.md) and
  [Meeting Cost Estimate](meeting-cost-estimate.md) — what each section's
  settings do
