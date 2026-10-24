---
title: "Unified Configuration Store"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Unified Configuration Store

LMA's runtime configuration — the things an administrator can change from the web
UI without redeploying the stack — has grown up one store at a time. There are
six of them, each with its own DynamoDB table or SSM parameter, its own seeding
custom resource, its own resolver and its own idea of how long a value may be
cached. This page describes the single store that replaces them, and the state of
the migration into it.

**Current state:** the store, the accessor and the deploy-time seeder are in
place, and the **summary prompt templates** are served from them. The other five
stores still work exactly as before and are migrated in later releases.

## The table

One DynamoDB table, `ConfigurationTable`, created by the AI stack. A single HASH
key named `Configuration`, no sort key and no secondary indexes. Everything the
store holds is distinguished by a prefix on that one key:

| Key | Contents |
|---|---|
| `Config#default` | The deployment baseline. CloudFormation writes it on create and rewrites it whenever the shipped content or the code that writes it changes, so it holds the content the installed release ships. |
| `Config#<name>` | A profile: an administrator's edits, stored as a *delta* over the baseline. Today there is one, `Config#custom`. |
| `Config#__active` | A pointer naming the profile in force. |
| `Schema` | The form schema a configuration UI renders. Reserved; written by a later release. |
| `ConfigRevIndex#<name>` | Revision metadata. Reserved; written by a later release. |

Two details in that layout are load-bearing rather than cosmetic.

The revision index deliberately sits **outside** the `Config#` prefix. Listing
profiles is a `begins_with(Configuration, "Config#")` query, and an index sharing
that prefix would come back from it looking like a profile.

The active pointer *does* share the prefix, so it is excluded from that listing by
name, and the names `default`, `__active` and `schema` — along with anything
beginning `__` — are refused at write time. A profile that could be created under
the name `__active` would be a profile that overwrites the pointer. Profile names
are restricted to `[A-Za-z0-9._-]`, at most 64 characters, because a name appears
verbatim inside the partition key and, from the revision work onwards, inside an
S3 object key as well.

### How a record is stored

The body of a record — everything an administrator edits — is serialised to JSON,
gzipped and stored in one Binary attribute, `Body`. A short, closed list of
*metadata* attributes stays at the top level in plain form, so a record's
provenance can be read without decompressing anything:

```
Configuration  CreatedAt  UpdatedAt  UpdatedBy  Version
IsActive  ActiveProfile  Description  Managed  ProfileOrigin
LatestRevision  PublishedRevision
```

`ProfileOrigin` is the one of those that does something an operator may act on:
it records how a profile came to exist, is written once when it is created, and
is carried forward by every later write. It is what the
`ConfigurationProfileOrigin` stack output reports.

`UpdatedBy` and `UpdatedAt` are the audit attributes the existing configuration
records already carry; they mean the same thing here.

Reads **auto-detect** the layout. An item with a `Body` attribute is
decompressed; an item without one is an *inline* record, and its remaining
top-level attributes are read as its body.

Nothing in LMA writes an inline record — the seeder and the resolvers both
compress — so the detection is there to let one be *read*. Two cases call for
that. A later store migration can lift an existing table's items into this one
as they stand, rather than needing a conversion pass before anything can read
them. And a record repaired by hand in the DynamoDB console is read as what it
says rather than as empty. The first ordinary write converts such a record.

Note the asymmetry, because it is the thing most likely to surprise someone at a
DynamoDB console: when `Body` is present it is the *whole* body, and any other
non-metadata attribute on the item is ignored rather than merged in. An attribute
added beside a compressed body has no effect at all.

Size is checked, not trimmed. DynamoDB's limit is 400 KB per item; a record
over 350 KB logs a warning, and one over the limit raises. A
configuration silently missing its tail is much harder to diagnose than a save
that refuses.

## The accessor, and the caching policy

There is one accessor, in two implementations because the consumers are in two
runtimes:

| Language | Path | Consumers |
|---|---|---|
| Python | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/` | The Lambda functions, via a Lambda layer |
| TypeScript | `lma-websocket-transcriber-stack/source/app/src/config/configuration-store.ts` | None yet — the WebSocket transcriber, and the Virtual Participant once the module is shared |

Both read the table through one environment variable, `CONFIGURATION_TABLE_NAME`.

The Virtual Participant is a separate npm package and cannot import the
transcriber's module as things stand, so the stage that migrates its first
setting has to move the module somewhere both can reach or copy it. That is a
packaging decision worth making deliberately rather than discovering: there is no
shared TypeScript package in LMA today, which is why there are two
implementations of this accessor rather than one.
`lma-ai-stack/test/test_configuration_store_contract.py` asserts, across the
language boundary, that the two halves agree on the key layout, the metadata list
and every cache tier — the drift between them would otherwise be silent, because
a configuration read as empty is indistinguishable from one nobody has set.

### Three tiers, declared per section

The stores being replaced use four different caching policies between them, and
two **readers in one directory of the transcriber are deliberately opposite**:
the ASR switches are cached for 30 seconds so that a burst of meetings starting
together does not each issue its own read, while the voice-enrollment reader —
not one of the six stores, but the clearest case of the principle — never caches
at all, because a withdrawal of consent is promised to take effect for every
later meeting and a cache is exactly a window in which it would not.

Both of those are right for what they read. So the caching policy is a property
of each **section** of the configuration document, declared once where the
section is declared, rather than a global setting or a call-site argument:

| Tier | TTL | Read | For |
|---|---|---|---|
| `immediate` | none | `ConsistentRead` | Anything the UI promises takes effect at once, and anything consent- or privacy-bearing |
| `short` | 30 s | eventually consistent | Per-meeting switches read once at session start |
| `standard` | 300 s + fingerprint | eventually consistent | Prompts, model ids, numeric limits |

The declaration is enforced, not merely documented:

- Reading a section that is not registered **raises** rather than falling back to
  a default policy. The alternative to a declared tier is an error.
- No read API takes a TTL, cache or max-age argument. The Python half's test
  suite asserts that by reading its own signatures, and the cross-language
  contract test asserts the same of the TypeScript half. That single property is
  what stops the design regressing into the four disagreeing policies it
  replaces.
- `immediate` means *no cache*, not a very short one: a cached document is never
  served for it, however little time has passed, and its reads are strongly
  consistent.

The `standard` tier also exposes a **fingerprint** — a short digest of the
section's resolved values. A reader whose work is expensive to rebuild (a
compiled template list, a connected client) can compare fingerprints and tell a
refetch that changed nothing from one that did. This is the policy the MCP server
loader already uses, generalised.

### Resolving a read

`get_section` / `getSection` resolves the document before returning a section:
the baseline `Config#default`, with the active profile's body deep-merged over
it. A value in the profile replaces the one in the baseline, except where both
are objects, in which case they merge key by key. That is what makes a profile a
delta — it holds only what an administrator changed, and everything else keeps
coming from the release.

A pointer naming a profile that is not stored resolves to the baseline alone
rather than to nothing, so deleting a profile cannot take a feature down with it.

A document whose baseline record is **not** there is never cached, whatever tier
is being read. A stack update can put new code live a few seconds before the
seeding custom resource runs, and caching the empty document that produces would
hold that empty answer for the whole of a tier's TTL *after* seeding had
finished — turning seconds into minutes. The resources that read configuration
are also ordered behind the seeder with `DependsOn`, so both halves of that are
covered.

### Keeping a profile a delta

A profile is only a delta if what writes it sends one. The Transcript Summary
page stores a section only when its prompt differs from the baseline's, so
saving the page without editing anything stores nothing, and a later release that
improves a prompt nobody touched still reaches the deployment. Removing a
section is the case that needs saying explicitly: leaving it out would let the
baseline supply it again, so a removal is stored as `NONE`, which the summarizer
skips.

Prompts adopted from the previous table on upgrade are made into a delta the
same way. What was stored there was the *whole* list — the page that wrote it
saved every section on every save — under numbering that followed its own list
rather than the release's, so adoption realigns each prompt onto the baseline
key with the same section title and drops anything identical to the prompt this
release ships. A record holding nothing but shipped prompts adopts nothing at
all.

One prompt is kept that arguably should not be: one that *differs* from the
current baseline, even though a release having since changed that section means
it may be the previous shipped text rather than anything anybody wrote. The two
are indistinguishable at that point, and keeping text an operator may have
written is the safer error. The next save from the page drops it if it has since
come to match.

## Writing: no more last-save-wins

Every one of the existing stores writes whole items with no condition, so when two
administrators have the same page open the second save replaces the first and
nothing records that it happened.

Writes through the accessor are conditional. A client reads a record, gets its
`Version`, and sends that version back when it saves. If the stored version is
not the one the client read, the write is refused with a message telling the
administrator to reload and reapply their change. Three conditions cover the three
states a record can be in:

| The record the caller read | Condition on the write |
|---|---|
| Absent | `attribute_not_exists(Configuration)` |
| Present but inline, so carrying no `Version` | `attribute_not_exists(Version)` |
| Present and versioned | `Version = <the version read>` |

The middle row exists because "no version" and "no record" are not the same thing
and both read as version 0. It applies only to an inline record — one lifted in by
a later store migration, or repaired by hand — since everything the accessor writes
carries a version. Conditioning that write on the record being absent would refuse
the save outright; conditioning it on the record still being un-versioned lets the
save through and still refuses a second one that a concurrent save got in ahead of.

The version is optional on the GraphQL input, so a client that does not send it
keeps working. Its save is still conditional on the record as the resolver read
it at save time, so a save landing in that window is not lost; what it does not
get is the check against the version the *client* loaded, so it can still
replace an edit that was saved after its page was drawn. Sending
`ExpectedVersion` adds that check, and is what produces the message naming who
saved the record.

That distinction is deliberate and it is why there are two write methods on the
accessor. A save that builds its body from the record it just read — which is
every save of one section of a profile — cannot be unconditional without being
the lost-update pattern, so it never is. A save that replaces a whole body and
reads nothing to build it, such as an import, may opt out.

The baseline `Config#default` is written unconditionally by design: CloudFormation
is its only author, and a new release's content has to land.

## Deploy-time seeding

A custom resource, `SeedConfiguration`, backed by
`lma-ai-stack/source/lambda_functions/configuration_seeder/`. It makes three
writes with different rules about what each may overwrite:

- **`Config#default`** — written on create, and rewritten on every update on
  which the custom resource runs at all, which is every update that changes the
  shipped content or the code that writes it (see the content hash below). It is
  this deployment's own content.
- **`Config#custom`** — written **only if it is not already there**, so a stack
  update never discards an edit made from the web UI. The condition is on the
  record, not on `RequestType == 'Create'`: keying off the request type would
  also skip the case where the record is genuinely absent on an update, which is
  what happens after a table restore or a hand deletion.
- **`Config#__active`** — also written only if absent, so an administrator who
  selects a different profile keeps their choice.

### Why the content hash exists

CloudFormation re-invokes a custom resource on UPDATE **only when one of its
properties changes**. A seeding resource whose properties are constant is
therefore invoked once, on the deployment's first create, and never again — so a
release that changed the shipped prompts would never deliver them to an existing
stack.

`SeedConfiguration` therefore carries a `SeedContentHash` property. The publish
step computes it (`calculate_configuration_seed_hash` in the LMA SDK) over the
shipped content **and** over the code that processes it, substitutes it into
`<CONFIGURATION_SEED_HASH_TOKEN>` in `lma-main.yaml`, and the AI stack passes it
through. Hashing the content alone would miss a change to how it is written,
which is just as much a reason to re-run the seeder.

The hash covers those two directories whole, including their test modules, so a
release whose only change there was to a test still shows `SeedConfiguration`
updating in the stack events. That is deliberate: the alternative — a filter
that decides what counts — can exclude a real change, and that failure is the
silent one. A re-run rewrites the baseline with identical content and leaves
every profile untouched.

## What is served from the store today

The **summary prompt templates** — the prompts behind each section of an
end-of-meeting summary, edited on the Transcript Summary page. They were chosen
as the first migration because they exercise every part of the design: they are
long enough for the compressed body to matter, they are seeded at deploy time and
edited in the UI (so the "a stack update must not overwrite an edit" rule is a
real requirement rather than a hypothetical one), they were read with no caching
at all (two fresh `GetItem` calls per summary), and they are exactly the kind of
value the `standard` tier is for.

Under the hood:

- `Config#default` holds `summary_prompt_templates` built from the JSON shipped
  with the seeder, keyed `N#Title` where `N` orders the sections of the summary —
  the same shape as before.
- `Config#custom` holds the administrator's edits as a delta.
- The meeting-summary Lambda reads a section rather than assembling two whole
  items itself, and a warm container now reads DynamoDB at most once per five
  minutes rather than on every summary. An uncached read is three `GetItem`
  calls — the active pointer, the baseline, and the active profile — so the
  saving is the cache, not the call count.
- **Regenerate** is the exception to that cache. The Meeting Summary panel
  promises that pressing it uses the prompt templates as they are now, so the
  regenerator's invoke carries `RefreshConfiguration` and the summariser drops
  its cached copy before reading. Saving a prompt and pressing Regenerate
  immediately therefore uses the prompt that was just saved. An end-of-meeting
  run, which nobody is waiting on, is served from the cache.
- `Query.getLLMPromptTemplate` and `Mutation.updateLLMPromptTemplate` are both
  served by one Lambda resolver. The read moved off its DynamoDB data source
  because the body is compressed and an AppSync JavaScript resolver has no way
  to decompress one. The field still answers with a JSON string, as it always
  has, but what is in it is now the record's prompts plus its `Version`,
  `UpdatedBy` and `UpdatedAt` — rather than the whole DynamoDB item, which also
  carried the partition key and the seeded guidance text. The UI read neither of
  those; another client that did needs to stop.

### Upgrading an existing deployment

Nothing to do, in the ordinary case. On the first deployment that includes this
change, the seeder reads the prompt templates from the previous table and carries
the customised ones into `Config#custom` — realigned onto this release's section
numbering, and with anything identical to a shipped prompt left out so the
profile is a delta from the start. The upgrade is not experienced as a reset.

**One case to check after the upgrade.** If the seeder cannot read the previous
table, the deployment still succeeds and the profile starts empty, which means
the shipped prompts apply. That is a far better outcome than a stack that rolls
back, but it is also a one-shot: the empty profile is now there, so no later
deployment tries the adoption again. The stack's
**`ConfigurationProfileOrigin`** output says which happened:

| Value | Meaning |
|---|---|
| `created` | A new, empty profile — the ordinary case for a new deployment, and for an upgraded one whose prompts were never customised. |
| `created-from-previous-table` | Customised prompts were carried across. |
| `created-previous-table-unreadable` | **Act on this one.** Prompt templates customised before the upgrade have to be applied again from the Transcript Summary page. The previous table is retained, so the old values can be read from it. |
| `unknown` | The profile was written by something other than the seeder. |

It is a stack output because a custom resource's response does not appear in
the stack events, and this is the one outcome somebody has to do something
about. The value records how the profile came to **exist**: it is stamped on the
record when the profile is created and carried forward by every later write,
including an administrator's save, so it reads the same however many
deployments have run since — and the seeder runs on every release that changes
either of its directories.

The previous table is left in place and still seeded by the
`lma-llm-template-setup-stack`. The summary prompts in it are read only by the
configuration seeder, and only when `Config#custom` is absent; they stay there so
a deployment can be rolled back to an earlier release.

The table is not otherwise retired, because it also holds the
[troubleshooting agent's](troubleshooting-agent.md) system prompt — the
`DefaultTroubleshootingAgentPrompt` and `CustomTroubleshootingAgentPrompt`
items, seeded by the same stack and read by the agent directly. That prompt has
not been moved into this store: it stays at the same two keys, the
configuration seeder can only read that table and does not adopt those items,
and `lma-ai-stack/test/test_troubleshooting_prompt_survives_the_store_migration.py`
holds both of those true. Moving it in as a second section is one of the
migrations still to come.

One thing to know before rolling back, because it is not the obvious thing: a
prompt edit saved *after* the upgrade exists only in `ConfigurationTable`, and
rolling back does **not** park it somewhere the next upgrade picks up again.

Rolling the stack back to a release whose template has no `ConfigurationTable`
removes that resource. With `EnableDataRetentionOnDelete=true` the table is
retained and orphaned; with `EnableDataRetentionOnDelete=false` it is deleted
along with the edits. Either way the summariser goes back to reading the
previous per-store table, which still holds the prompts as they were before the
upgrade.

Deploying the newer release again does not reconnect the old data. The resource
declares no fixed `TableName`, so a new, empty table is created, and the seeder
fills `Config#custom` from the previous per-store table — the pre-upgrade
prompts again. **Any prompt edit made between the upgrade and the rollback has
to be applied again from the Transcript Summary page**; if retention was on, the
orphaned table is where to read the old values from.

## Still to come

| Stage | What it adds |
|---|---|
| Revisions and audit | Revision bodies in S3 with metadata in one DynamoDB index item per profile, forward-only restore, pruning that keeps the published and labelled revisions |
| Import / export | Export as a client-side download; import that always routes into a create-new-profile flow rather than applying in place |
| Schema-driven UI | A profile table, a form generated from the `Schema` document, a diff against the baseline and per-field "restore default" |
| Migrating the other five stores | The chat-button config, the Nova Sonic config, the ASR switches, the MCP server list, and the SSM settings blob |
| Moving the troubleshooting agent's prompt | Its `Default`/`Custom` pair in `LLMPromptTemplateTable` becomes a section here; then that table can be retired |
| Moving CloudFormation parameters | The parameters that are read only at request time and do not gate a resource, a condition or an IAM policy |

One known gap belongs to that third row and is worth stating rather than leaving
to be discovered. A shipped prompt section that has been turned off is hidden
from the Transcript Summary page, which is right — the page shows what will run
— but it means the only way to bring that one section back is **Reset to
Defaults**, which discards every other edit as well. Re-adding it by hand
usually gives it a new sequence number and leaves the suppression behind — the
page numbers a new section above the highest one it can see, so it comes back
under its own key only when the section that was turned off was the last one.
The per-field "restore default" control in the schema-driven editor is where
that belongs.

## Reference

| What | Where |
|---|---|
| Python accessor | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/` |
| TypeScript accessor | `lma-websocket-transcriber-stack/source/app/src/config/configuration-store.ts` |
| Deploy-time seeder | `lma-ai-stack/source/lambda_functions/configuration_seeder/` |
| Table, layer and custom resource | `lma-ai-stack/deployment/lma-ai-stack.yaml` |
| Content-hash computation | `lib/lma_sdk/lma_sdk/_core/publish.py` |
| Cross-language contract test | `lma-ai-stack/test/test_configuration_store_contract.py` |
