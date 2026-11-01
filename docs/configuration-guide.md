---
title: "Configuration Guide"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Configuration Guide

This page is for administrators. It explains which of LMA's settings you can
change from the web UI while the stack is running, what happens when you save
one, and what to do when a save is refused. How the settings are stored, cached
and versioned underneath is described in the
[Unified Configuration Store](configuration-store.md) reference.

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

| Section | Where you edit it | What it controls | A saved change applies |
|---|---|---|---|
| Transcript summary prompts | **Configuration ▸ Transcript Summary** | The sections of the end-of-meeting summary and the prompt behind each | To summaries generated within about five minutes of the save. **Regenerate** on a meeting page uses it at once |
| Meeting Assist chat buttons | **Edit Chat Buttons**, from the chat panel on a meeting page | The quick-action buttons in the chat panel. A saved list replaces the shipped buttons as a whole | At once, to chat panels opened after the save |
| Nova Sonic voice assistant | **Configuration ▸ Nova Sonic Config** | The voice assistant's system prompt and prompt mode, model, voice, turn-taking sensitivity, meeting mode and translator settings | To the next meeting the voice assistant joins |
| Transcription engine | **Configuration ▸ Transcription Engine**, shown only when the on-demand speech engine is deployed | Which engine transcribes streaming meetings and Virtual Participants, and Virtual Participant voice separation | To meetings that start within 30 seconds of the save or later |
| Meeting cost prices | The `updateCallPricing` GraphQL operation; there is no page for it yet | The unit prices the per-meeting [cost estimate](meeting-cost-estimate.md) is computed with | To estimates drawn within about five minutes of the save |

Only members of the **Admin** Cognito group can change any of these. Add someone
to the group on [User Management](user-management.md) if they need to.

## What happens when you save

**The save is checked before anything is stored.** Every section has a schema
that says which settings it has and what values each accepts: a Nova Sonic
endpointing sensitivity is `LOW`, `MEDIUM` or `HIGH`; a price is a number that is
not negative; a summary section's title starts with its number. A save that
breaks any of those rules is refused as a whole, nothing is stored, and the error
names each setting that was wrong. For example:

```
call_pricing › regions › default › transcribe/streaming › seconds: must be at least 0; got -1
```

Read the path from left to right: the section, then each key down to the setting.
Correct the value and save again.

**The save is checked against the version you loaded.** All the sections are
stored in one profile, and every save is conditional on the profile still being
the one you loaded. If another administrator saved any configuration in between —
on the same page or a different one — your save is refused with a message to
reload, rather than silently replacing their change. Reload, apply your change
again and save.

**Every save is recorded.** Each save that changes something becomes a numbered
revision recording who saved it, when, and which sections it changed. Open
**Configuration ▸ Configuration History** to see the revisions, compare any two
of them setting by setting, or restore an earlier one. A restore puts back every
section as it was in that revision and is itself recorded as a new revision, so
it can be undone the same way. See
[Revisions and audit](configuration-store.md#revisions-and-audit).

## Changing a setting outside the web UI

Every page above saves through a GraphQL operation that an Admin can also call
directly, with the same checks; they are listed in the
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
