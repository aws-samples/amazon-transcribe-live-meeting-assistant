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
cached. This page describes the single store five of them move into, and the
state of the migration into it; the sixth, the MCP server list, stays a table of
its own, for the reason under [Still to come](#still-to-come).

**Current state:** the store, the accessor and the deploy-time seeder are in
place, and the **summary prompt templates**, the **Nova Sonic voice assistant
settings**, the **Meeting Assist chat buttons** and the **ASR engine switches** are
served from them, alongside the meeting cost estimate's price table, which had no
earlier store, and the settings that were stack parameters until 0.4.0, such as
the Amazon Bedrock model choices; see
[Settings that were stack parameters until 0.4.0](#settings-that-were-stack-parameters-until-040). Every save into the store records a revision — who saved it,
when, which sections it changed, and an optional comment — and an Admin can list,
compare and restore revisions; see [Revisions and audit](#revisions-and-audit).
Each section has a JSON Schema document, and every administrator's save is
checked against it before anything is written; see
[Section schemas and validation](#section-schemas-and-validation). The
documents also carry the layout hints the web UI's **Configuration** page renders
its form from; see [The configuration editor](#the-configuration-editor). The
SSM settings blob moves later, with the CloudFormation parameters; see
[Still to come](#still-to-come) for that, for the MCP server list, and for the
troubleshooting agent's prompt, which shares the summary prompts' old table and
is the next store to move in.

## The table

One DynamoDB table, `ConfigurationTable`, created by the AI stack. A single HASH
key named `Configuration`, no sort key and no secondary indexes. Everything the
store holds is distinguished by a prefix on that one key:

| Key | Contents |
|---|---|
| `Config#default` | The deployment baseline. CloudFormation writes it on create and rewrites it whenever the shipped content or the code that writes it changes, so it holds the content the installed release ships. |
| `Config#<name>` | A profile. A **Custom** profile is an administrator's configuration, stored as a full copy (`ProfileFormat: full`); `Config#custom` is the one a deployment creates. A **Managed** profile is written by the deployment and is read-only. See [Named profiles](#named-profiles). |
| `Config#__active` | A pointer naming the profile in force. |
| `Schema` | Reserved, and not written. The section schemas ship inside the configuration layer instead, so a save is always checked against the schema its readers were released with; see [Section schemas and validation](#section-schemas-and-validation). |
| `ConfigRevIndex#<name>` | The revision index of a profile: one small entry per retained revision. See [Revisions and audit](#revisions-and-audit). |

Two details in that layout are load-bearing rather than cosmetic.

The revision index deliberately sits **outside** the `Config#` prefix. Listing
profiles is a `begins_with(Configuration, "Config#")` query, and an index sharing
that prefix would come back from it looking like a profile.

The active pointer *does* share the prefix, so it is excluded from that listing by
name, and the names `default`, `__active` and `schema` — along with anything
beginning `__` — are refused at write time. A profile that could be created under
the name `__active` would be a profile that overwrites the pointer. Profile names
are restricted to `[A-Za-z0-9._-]`, at most 64 characters, because a name appears
verbatim inside the partition key and inside the S3 key of each of its
revisions.

### How a record is stored

The body of a record — everything an administrator edits — is serialised to JSON,
gzipped and stored in one Binary attribute, `Body`. A short, closed list of
*metadata* attributes stays at the top level in plain form, so a record's
provenance can be read without decompressing anything:

```
Configuration  CreatedAt  UpdatedAt  UpdatedBy  Version
IsActive  ActiveProfile  Description  Managed  ProfileOrigin
ProfileFormat  LatestRevision  PublishedRevision
```

`ProfileOrigin` is the one of those that does something an operator may act on:
it records how a profile came to exist, is written once when it is created, and
is carried forward by every later write. It is what the
`ConfigurationProfileOrigin` stack output reports.

`UpdatedBy` and `UpdatedAt` are the audit attributes the existing configuration
records already carry; they mean the same thing here.

`LatestRevision` and `PublishedRevision` are a profile's revision counters: the
highest revision number allocated for it, and the revision its body currently
is. Every write carries them forward, including a write made by code that keeps
no history, so a number is never handed out twice. See
[Revisions and audit](#revisions-and-audit).

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

## Named profiles

The table holds any number of profiles. One is active, named by the
`Config#__active` pointer, and every reader resolves the baseline with the
active profile merged over it. The pointer may also name `default`, in which
case nothing is merged over the baseline.

- **Custom profiles** are full copies of the configuration. A new one is a copy
  of another profile, the baseline by default, or an imported configuration
  laid over the baseline, and every save stores each section in full: a section
  sent with only some of its settings is completed from the baseline. The
  profile is still merged over the baseline when it is read, so a setting a
  later release adds has a value in every profile.
- **The baseline and Managed profiles are read-only.** The deployment rewrites
  them on every stack update, so the store refuses to save, restore or delete
  them; copy one to change it.
- **The active profile is not deleted.** Activate another first. A deleted
  profile's revisions stay in its history, and a profile created again under the
  same name continues its numbering.
- **A deployment runs on its default profile until an Admin changes
  something**, as IDP's do: the baseline is rewritten by every release, so an
  unchanged deployment gets each release's defaults. To change a setting, an
  Admin creates a Custom profile (a copy of the default, or of a Managed preset
  profile) and activates it.
- **The pages that edit one section** (the summary prompts, the Nova Sonic
  voice assistant, the chat buttons and the ASR engine switches) show the
  active profile and save into it. While the default or a Managed profile is
  active they refuse to save, with a message to create or activate a Custom
  profile.
- **Upgrading from a release with one `custom` profile.** The seeder settles
  it after its other writes: a `custom` profile holding settings (an Admin's
  edits, or ones adopted from a previous table) is converted to a full copy and
  stays active, every setting it held kept and the rest filled from the new
  baseline, with the delta it was kept in its history as the revision before;
  one holding none is deleted and the default activated. The seeder reports
  this as `ProfileFormat`: `converted`, `removed-empty`, `full` or `absent` on
  later runs, and `deferred` when it has to wait -- a section adoption is to be
  retried, or the profile's state could not be recorded in its history -- in
  which case readers keep using the delta and the next deployment tries again.
- **Deleting and activating at once.** A delete checks that the profile is not
  the active one and then deletes it; an activation of the same profile landing
  between the two leaves the pointer naming a profile that is not there, and
  readers then use the default, as for any pointer naming a missing profile.

The `listConfigurationProfiles` query and the `createConfigurationProfile`,
`deleteConfigurationProfile` and `activateConfigurationProfile` mutations, all
for the Admin group, serve them; `getConfiguration(Profile)` reads any of them,
the baseline included, and reports whether it is Managed and whether it is
active.

## The accessor, and the caching policy

There is one accessor, in two implementations because the consumers are in two
runtimes:

| Language | Path | Consumers |
|---|---|---|
| Python | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/` | The Lambda functions, via a Lambda layer |
| TypeScript | `lma-websocket-transcriber-stack/source/app/src/config/configuration-store.ts`, copied verbatim to `lma-virtual-participant-stack/backend/src/configuration-store.ts` | The Virtual Participant, for the Nova Sonic settings |

Both read the table through one environment variable, `CONFIGURATION_TABLE_NAME`.

The Virtual Participant is a separate npm package and cannot import the
transcriber's module, and there is no shared TypeScript package in LMA, so the
TypeScript half is **copied**: the transcriber's file is the one edited, and the
Virtual Participant's is a byte-for-byte copy of it. The file imports nothing
relative, so it compiles unchanged under the transcriber's CommonJS build and the
Virtual Participant's ES module one. A shared package would remove the copy, at
the cost of a build step both packages' Docker images would then depend on; at
one file, the copy is the smaller thing to keep right.

`lma-ai-stack/test/test_configuration_store_contract.py` keeps both kinds of
duplication honest. It asserts that the two TypeScript copies are identical, and,
across the language boundary, that the Python and TypeScript halves agree on the
key layout, the metadata list and every cache tier — the drift between them
would otherwise be silent, because a configuration read as empty is
indistinguishable from one nobody has set.

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

Today's sections and their tiers:

| Section | Tier | Why |
|---|---|---|
| `summary_prompt_templates` | `standard` | Read by an end-of-meeting summary nobody is waiting on; **Regenerate** drops the cache before reading |
| `nova_sonic_config` | `immediate` | Read once per meeting when the voice assistant starts, and the page says a save applies to the next meeting; a cache would save a read or two per meeting and add a window in which that is not true |
| `asr_engine` | `short` | Read by the WebSocket transcriber at the start of each streaming meeting; 30 seconds keeps a burst of meetings starting together from each doing its own reads, which is the policy that reader has always had. The Virtual Participant reads through the `getAsrConfig` resolver, which caches nothing |
| `chat_buttons` | `immediate` | Read record by record through the chat panel's resolver, which reads directly and consistently; the Edit Chat Buttons dialog shows the saved buttons straight after a save. The browser holds what it read for the life of the chat panel, as it always has |
| `call_pricing` | `standard` | Read whenever a meeting's cost estimate is drawn; an estimate priced a few minutes after a price change at the previous price is what [Meeting Cost Estimate](meeting-cost-estimate.md) says happens |
| `transcription` | `immediate` | Read by the WebSocket transcriber once as each streaming meeting starts, and kept for the meeting's every Transcribe reconnect. Redaction is privacy-bearing, so a save has to reach the very next meeting |
| `recording` | `immediate` | Read with `transcription`, for the same reason: whether a meeting is recorded is privacy-bearing |
| `meeting_assist` | `standard` | Read by the call event processor for each batch of transcript segments |
| `meeting_processing` | `standard` | Read by the call event processor for each segment it hands to a transcript Lambda hook, and by the inactive-meeting reaper as each run starts |
| `virtual_participant` | `immediate` | Read once by each Virtual Participant as it starts, with the `transcription` and `recording` sections, as for the Nova Sonic settings it reads at the same moment; the page says a change applies to the next one to start |
| `language_models` | `standard` | Read once per summary, answer, agenda pass, naming pass, knowledge-base query or key frame; nobody is waiting on a model change, and the summariser's **Regenerate** drops the cache before reading, as for the prompts |
| `meeting_consent` | `immediate` | Read by the meeting consent resolver when a meeting's consent starts and whenever a meeting's consent is read, and by the WebSocket transcriber when a streamed meeting starts and when a client asks for the consent step's settings. Consent-bearing: an administrator who turns opt-in on is promised that the next meeting asks first. If it cannot be read, starting a meeting's consent, and reading a meeting with no consent record, fail rather than fall back to off; a restart, and a read of a meeting with a record, do not need it. See [Meeting Consent](meeting-consent.md#the-meeting-consent-settings) |

### Resolving a read

`get_section` / `getSection` resolves the document before returning a section:
the baseline `Config#default`, with the active profile's body deep-merged over
it. A value in the profile replaces the one in the baseline, except where both
are objects, in which case they merge key by key. That is what makes a profile a
delta — it holds only what an administrator changed, and everything else keeps
coming from the release.

A pointer naming a profile that is not stored resolves to the baseline alone
rather than to nothing, so deleting a profile cannot take a feature down with it.

The TypeScript half also returns the two layers a section was merged from — the
baseline's section and the active profile's — beside the merged values. That is
for a reader whose settings do not simply override one another: in the Nova
Sonic settings' `inject` prompt mode the administrator's prompt is *appended* to
the shipped one, which a merged value has already lost.

A document whose baseline record is **not** there is never cached, whatever tier
is being read. A stack update can put new code live a few seconds before the
seeding custom resource runs, and caching the empty document that produces would
hold that empty answer for the whole of a tier's TTL *after* seeding had
finished — turning seconds into minutes. The resources that read configuration
are also ordered behind the seeder with `DependsOn`, so both halves of that are
covered.

### Keeping a profile a delta

A profile is only a delta if what writes it sends one. The Configuration page
stores a value only when it differs from the baseline's (see
[The configuration editor](#the-configuration-editor)), so saving without
editing anything stores nothing, and a later release that
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

## Section schemas and validation

Every registered section has one JSON Schema document,
`lma_config/schemas/<section>.json` in the configuration layer. The document does
two jobs:

- **It is what a save is checked against.** The store checks every section a save
  writes before it reads or writes anything, so a value its readers could not use
  is refused at the save rather than discovered when a meeting reads it.
- **It is what a configuration editor renders.** Beside the validation keywords,
  the documents carry layout annotations — `order`, `sectionLabel`,
  `collapsible`, `dependsOn`, `format: textarea` and the like — in the vocabulary
  the configuration schema of the
  [GenAI Intelligent Document Processing accelerator](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws)
  uses, and `Query.getConfiguration` serves them as they stand.

The schemas are standalone files rather than YAML inside a CloudFormation
template, and they ship inside the configuration layer. The layer's build is
what puts them in front of the code: the schema a save is checked against is
always the one released with the code that reads the section. The `Schema` key
the table reserves is therefore not written.

### What a schema describes

A schema describes the section **as a profile stores it**, which is a delta over
the baseline. That is why no section schema lists `required` properties at its
top level: a profile that changes one Nova Sonic setting stores that one setting,
and an empty section is what **Reset section to defaults** stores. The shipped baseline is
checked against the same documents by the layer's tests, for both prompt domains,
so a release cannot ship a default its own save would refuse.

| Section | What the schema holds it to |
|---|---|
| `summary_prompt_templates` | Keys of the form `N#Title`, with `N` in the digits 0–9 and a title that is not empty, each a text prompt; `NONE` turns a shipped section off |
| `chat_buttons` | Keys of the form `N#Label`, each a text prompt |
| `nova_sonic_config` | The eleven settings the voice assistant reads, nothing else; `promptMode`, `endpointingSensitivity` and `meetingMode` from their fixed lists; model and voice ids and the translator fields non-empty and bounded in length |
| `asr_engine` | The three switches, each `true` or `false`, nothing else |
| `language_models` | The Model inference geography (`global`, `geography` or `in_region`) and the four model ids, each one of the models its setting offers, nothing else |
| `transcription` | The nine Amazon Transcribe options: language, preferred language and redaction type from their stack parameters' lists; language options as comma-separated codes from that list; entity types as comma-separated capitalised names; vocabulary and language model names in Amazon Transcribe's character set, empty for none; the two switches `true` or `false` |
| `recording` | `recordAudio`, `true` or `false`, nothing else |
| `meeting_assist` | `wakePhraseRegex`, text of 1 to 256 characters; `promptCachingEnabled`, `true` or `false`; `assistantReasoningEffort`, `low`, `medium` or `high` |
| `meeting_processing` | `transcriptHookNonPartialOnly`, `true` or `false`; `inactiveMeetingLookbackDays`, a whole number from 1 to 90, and `inactiveMeetingTimeoutMinutes`, a whole number from 0 to 10080, their parameters' ranges |
| `meeting_agenda` | `autoDeriveAfterSeconds`, a whole number from 0 to 3600, the parameter's range |
| `retention` | `meetingRecordExpirationInDays` and `transcriptionExpirationInDays`, numbers of days of at least 0.1, each at most the baseline's value (`maximumIsDefault`, below); `transcriptDeletionAfterSummary`, one of `off`, `immediately`, `after_15_minutes`, `after_30_minutes` and `after_60_minutes` (see [Transcript Deletion After the Summary](transcript-deletion.md)) |
| `meeting_consent` | `mode` and `consentRule` from their fixed lists; the two notice texts of 1 to 2,000 characters; the two phrase lists of 1 to 1,000 characters; `allowPerMeetingOverride`, `keepEvidenceScreenshots`, `leaveOnObjection` and `readSpokenReplies`, `true` or `false`; `verifierModelId`, one of the image-capable models the Key frame text setting offers; `clientsWithoutConsent`, `accept_with_warning` or `reject`; nothing else |
| `virtual_participant` | The provider from its parameter's list; wake phrases as text; the activation duration, poll interval and poll counts as whole numbers in their parameters' ranges; the ElevenLabs agent and Simli face ids in the characters their services use, empty for none |
| `call_pricing` | Prices that are finite and not negative, under `regions › <region or default> › <service>/<api-or-model> › <unit>`; region codes in the form `us-east-1`; each price's source under `sources`, laid out the same way, with a `basis` of `price-list-api`, `pricing-page` or `estimate`, a `retrieved` date written `YYYY-MM-DD`, an `https://` `url` and text fields of bounded length; the lists and notes as lists and objects of text; `currency` `USD`; no keys the estimator does not read |

### The validator

`lma_config/schema.py` implements the subset of JSON Schema (2020-12) the
documents use, with the standard library only, so the layer keeps its property
of having no third-party dependencies:

| Keywords | Meaning |
|---|---|
| `type` | `object`, `array`, `string`, `number`, `integer`, `boolean`, `null`, or a list of them. A boolean is never a number, and `NaN` or `Infinity` — which Python's JSON reader accepts and JSON has no form for — is never a number either |
| `enum` | The value is one of the listed ones, compared as JSON compares them, so `true` is not `1` |
| `minimum`, `maximum` | Inclusive bounds on a number |
| `minLength`, `maxLength`, `pattern` | Text length in characters, and a regular expression that must match somewhere in the text |
| `properties`, `patternProperties`, `additionalProperties`, `propertyNames`, `required`, `maxProperties` | Object members, with the standard rule, and of `propertyNames` only its `pattern`: a member named in `properties` or matching a `patternProperties` pattern is checked against those, and only a member matching neither falls to `additionalProperties` |
| `items`, `minItems`, `maxItems` | List members, with `items` as one schema for every member, and length |

A keyword the validator does not implement is refused by the layer's tests rather
than ignored, because an ignored keyword is a constraint that looks enforced and
is not. The UI annotations are listed in the module as `UI_ANNOTATIONS`, and the
same test refuses a key that is in neither list, which catches a misspelt one.

One keyword, `maximumIsDefault: true`, is checked against the deployment's
baseline rather than against the value alone, so the validator above cannot
apply it; it is listed as `BASELINE_KEYWORDS`. It says the setting's default,
which the seeder writes from a stack parameter, is also its upper limit. The
store applies it to every save that writes such a section (`limit_problems`,
one extra read of the baseline), refusing a value above the baseline's with
`must be at most 90, the limit this deployment sets; got 120`; the editor applies
its copy (`limitProblems`) with the defaults it loaded. A setting the baseline
does not hold has no limit at the save, and its readers still apply the
parameter's value as one.

The web UI holds a JavaScript copy of the validator,
`lma-ai-stack/source/ui/src/components/configuration/schema-validation.js`, for a
configuration editor to check a value before it sends the save. The two copies
are held to one fixture, `lma-ai-stack/test/fixtures/configuration_schema_cases.json`:
each case is a schema, a value, and the exact problems both must report, in order,
and pytest and vitest each run every case. The UI's tests also compile every
pattern in the shipped schemas as a JavaScript regular expression, since a pattern
only one language accepted would make the two disagree. Where the two languages
read a pattern differently, the server's copy reads it as JavaScript does: `$`
matches only at the end of the text, where Python's would also match before a
final newline, and `.` does not match a carriage return or a Unicode line or
paragraph separator. It also writes numbers in messages as JavaScript does
(`-1`, `7e-8`). The server's check is the one that decides.

### Which writes are checked

| Write | Checked |
|---|---|
| `updateLLMPromptTemplate`, `updateNovaSonicConfig`, `updateChatButtonConfig`, `updateAsrConfig`, `updateCallPricing`, `updateConfiguration` | Yes, every section the save writes |
| What the deploy-time seeder writes or carries across from a previous table | No |
| `restoreConfigurationRevision` | No |

The seeder and a restore are not checked because what they write was accepted
when it was first saved, and refusing it would turn a deployment or a recovery
into a failure. That includes the limit of a retention setting: a restore can put
back a value above today's limit, and every reader of the setting still uses
the parameter's value in its place.

The four older configuration mutations still filter their input the way they
always have — dropping a field they do not know, a Nova Sonic setting outside its
list, a switch that is not a boolean — before the store sees it. What they keep
is then checked like any other save. In practice the schema adds these refusals
to those mutations:

- a chat button or summary prompt key whose label is empty (`3#`), which both
  resolvers' own filters accept, and a summary prompt key whose number uses digits
  other than `0`–`9`, which the summary prompt resolver's filter accepts;
- a chat button prompt that is not text;
- a Nova Sonic system prompt, model id or voice id that is not text, and a model
  id or voice id that is empty or longer than its limit (256 characters for a
  model id, 128 for a voice id). The Configuration page never sends an
  empty one, so this affects only a client calling the mutation directly.

`updateCallPricing` has no checks of its own; the schema is the whole of what it
enforces.

A refused save writes nothing at all, even when only one of several sections is
wrong, and its error names every problem with the path of the value it is about,
for example
`call_pricing › regions › default › transcribe/streaming › seconds: must be at least 0; got -1`.
The path separator is ` › ` rather than `/` or `.`, because keys here hold both.
The message spells out the first ten problems and counts the rest.
`updateConfiguration` answers such a refusal — a value that does not match its
schema, or a section name that has none — and only such a refusal, with an error
starting `Invalid: `, which the web UI's Configuration page matches on.

### The editor's read and save

Two GraphQL operations, both for the **Admin** group on the AppSync field and
again in the resolver Lambda behind them, which is the configuration history's:

| Operation | What it does |
|---|---|
| `Query.getConfiguration(Profile)` | Everything an editor needs in one read: `Sections`, a JSON list of each section's `Name`, `CacheTier` and `Schema`; `Default`, the baseline; `Custom`, what the profile stores (`{}` for `Profile: "default"`); the profile's `Version`, `UpdatedBy`, `UpdatedAt` and `PublishedRevision`; and whether it is `Managed` (the baseline, and the preset profiles), `Active` and a `FullCopy`, with its `Description`. Read consistently from the stored records, never from a cache. Only registered sections are returned |
| `Mutation.updateConfiguration(input: {Profile, Sections, ExpectedVersion, Comment})` | Saves every section named in `Sections`, a JSON object of section name to that section's complete stored values, in **one** write and one revision. Sections not named keep what the profile holds. `ExpectedVersion` is required, and a save against a profile someone has saved since is refused with `Conflict: `. Saves only into a profile that exists. Answers with the new `Version`, the `Revision` it recorded, and `Unchanged` when the save changed nothing |

`Profile` defaults to `custom` for both.

### The configuration editor

The web UI's **Configuration ▸ Configuration** page
(`/#/configuration/settings`, for Admin users) is built on those two operations.
It renders one section per schema, in each schema's `order`, and a form inside
each from the schema's properties and annotations: a select for an `enum`, a
long text box for `format: textarea`, suggestions for `examples`, a number field,
a switch or a pair of radio buttons for a boolean, a list editor for an `array`,
an editable map for an object whose members are named by the administrator, and
a list of entries for `numberedKeys`. A section or a field whose `availableWhen` setting
does not hold is not shown, and a field whose `dependsOn` does not hold is
hidden. A preset is offered only where every section and top-level setting it
names is shown. A section can open by itself from a link:
`/#/configuration/settings?section=<name>`.

What it edits is each section's **effective** values — the baseline with the
profile combined over it according to the schema's `mergeStrategy`, so what the
section's readers use — and it turns them back into a delta only when it saves.
Its rules, in `lma-ai-stack/source/ui/src/components/configuration/config-model.js`:

- **Only what differs is stored.** For a `merge` section the delta is every key
  the baseline lacks and every value that differs from it; a value put back to
  its default is left out, which is what keeps the release improving it. A
  `replace` section is stored whole, or as `{}` while it equals the shipped one.
- **Only sections that changed are sent**, each as its complete delta, in one
  `updateConfiguration`, so the save is one revision.
- **"Differs from default" is marked on every value**, with a badge and a rule
  beside it. A changed shipped value has **Reset to default**, which puts back
  the shipped value; an entry the release does not ship has **Remove** instead.
  In a `replace` list such as the chat buttons, which is compared as a whole,
  entries have no reset of their own. Each section also has **Reset section to
  defaults** and **Undo changes**. The section heading says when a saved change
  applies: from the cache tier, or from the schema's `appliesText` where the tier
  alone would mislead — the voice assistant reads its settings as it starts, so a
  meeting already running keeps the ones it started with.
- **A shipped entry of a `merge` map cannot be removed**, because leaving it out
  of the delta would let the baseline supply it again. It can be reset; and where
  the schema declares a `disabledValue` — the summary prompts' `NONE` — it can be
  turned off and back on. Renaming a shipped summary section turns the old title
  off and adds the new one under the same number. An entry the administrator
  added can be removed, and a `replace` list such as the chat buttons can be
  reordered, which renumbers it from 1.
- **Values are checked as they are edited**, by the editor's copy of the
  validator against the delta a save would send, and the save is not offered
  while any problem stands. The server checks again.
- **A conflict keeps the administrator's work if they ask.** When the save is
  refused with `Conflict: `, the page offers to reload and keep the changes: it
  reads the profile again, re-applies each of the administrator's changes — a
  value set or a key removed, relative to what the page loaded — over what is
  now saved, and lists every setting both saves changed, which now holds the
  administrator's value. A `replace` section is taken as one value, because a
  reorder renumbers every key: if the other save changed it, the
  administrator's whole list is kept and the section is listed. Nothing is saved
  until they save again.
- **Every save asks for an optional comment and label**, recorded on the revision
  the save creates; the label is set with `labelConfigurationRevision` straight
  after the save.

The pages that each edited one section — Transcript Summary, Nova Sonic Config
and Transcription Engine — were replaced by the Configuration page once it could
do everything they did, and their addresses (`/#/configuration/transcript-summary`,
`/#/configuration/nova-sonic`, `/#/configuration/asr`) redirect to their section.
The **Edit Chat Buttons** dialog in a meeting's chat panel stays, because it is used
in the context of a meeting; it saves the same section. The per-section GraphQL
operations stay too, because other readers use them: every chat panel reads
`getChatButtonConfig`, the Stream Audio and Upload Audio forms and the Virtual
Participant read `getAsrConfig`, and `getNovaSonicConfig` keeps its IAM
authorization for backend callers. Their saves are checked against the same
schemas.

### Export, import and the configuration library

The configuration editor's **Actions** menu moves configuration between
deployments, and applies named presets. None of the three saves anything by
itself: an import or a preset becomes unsaved changes on the page, marked and
checked like any edit, and is saved only when the administrator saves it, as one
revision that can be compared and restored like any other.

- **Export** is a download the browser makes; no API is involved. The file holds,
  for every section the deployment offers, what the profile *stores* — its
  changes over the release defaults — as saved, not the page's unsaved edits:

  ```json
  {
    "format": "lma-configuration-profile",
    "formatVersion": 1,
    "exportedAt": "2026-11-01T12:00:00.000Z",
    "stackName": "LMA-prod",
    "profile": "custom",
    "revision": 12,
    "sections": { "nova_sonic_config": { "voiceId": "matthew" }, "call_pricing": {} }
  }
  ```

  Exporting the changes rather than the merged configuration is what makes a file
  portable between releases: imported into a deployment of a later release, it
  gives that release's defaults with the same changes over them.
- **Import** reads such a file, refusing anything that is not one, and sets each
  section it holds to what the file stores, over this deployment's defaults. A
  section the deployment does not offer is reported and left out. The save
  comment is filled in with the file's name.
- **Presets** are the configuration library: named, shipped organizational
  defaults for consent, retention and privacy, each
  `lma_config/presets/<id>.json` in the configuration layer, served by
  `getConfiguration` as `Presets`. A preset names the sections it changes, each
  with the mode `merge`, which lays its values over the section as it stands, so
  the settings it names change and every other one stays. A setting the
  deployment does not offer is left out. Every preset sets the same settings,
  which the layer checks when it loads them, so applying one sets all of them
  whichever was applied before. A preset may have an integer `order`, served as
  `Order`, which places it in the list the editor shows. The presets are the
  `ConfigurationPreset` stack parameter's choices and are written as Managed
  profiles. The layer's tests hold every preset to that shape, its values to the
  section schemas and to the values the
  [Configuration Guide](configuration-guide.md#configuration-presets) lists, and
  Standard to the shipped defaults.

| Preset | What it changes |
|---|---|
| Standard | The consent, retention and privacy settings the presets set, at their shipped defaults |
| Internal meetings – notify participants | Meeting Consent mode Notify; audio recorded and the meeting chat captured; the rest at their defaults |
| Regulated – consent required, transcript not retained | Meeting Consent mode Opt-in, stricter-only per meeting, clients without a consent step refused, the Virtual Participant leaving after an objection; the transcript deleted 15 minutes after the summary; no audio recording or chat capture; redaction on; Amazon Bedrock within the deployment's geography |
| Regulated – notify with opt-out, transcript not retained | As Regulated, except Meeting Consent mode Notify: capture from the start, stopped by an objection |

**Why import does not create a profile.** The plan for this store has an import
route into a *new* profile rather than apply in place, so that importing cannot
overwrite the configuration in force. LMA has one profile, `custom`, which every
reader resolves, and nothing yet selects another. A second profile would be a
record that nothing reads. The same protection comes from the import never saving:
the administrator sees every change it makes, marked against the defaults, before
anything is stored, and the save that stores it is one revision a restore takes
back. Named profiles, with a way to choose the active one, remain to come.

## Revisions and audit

Every write that changes a profile's body records a **revision**: a numbered,
immutable copy of the whole profile as it was saved, together with who saved it,
when, which sections it changed and an optional comment. That covers every route
into the store: each configuration page's save, `updateCallPricing`, a restore,
and what the deploy-time seeder writes into a profile. The baseline
`Config#default` has no history of its own. CloudFormation rewrites it from the
release, so the release is its history.

### What a revision records

| Field | Meaning |
|---|---|
| `Revision` | The revision number, counting up from 1 per profile |
| `CreatedAt` | When it was saved, ISO-8601 UTC |
| `CreatedBy` | Who saved it: the same identity the record holds as `UpdatedBy` — the saver's email address, or their Cognito username where the token carries none — taken from the verified token, never from the request. `cloudformation` for what the seeder writes |
| `Sections` | The sections this revision changed relative to the one before it. For the first revision of a profile, every section it holds |
| `Comment` | Optional, at most 500 characters. Every configuration save mutation accepts a `Comment` input; the Configuration page asks for one on every save, and a restore can send one |
| `Source` | `save`, `restore`, `deploy` (the seeder), `backfill` (the profile as it stood when history began) or `recovered` (see below) |
| `RestoredFrom` | For a restore, the revision whose body it restored |
| `Label` | Optional, at most 100 characters. A labelled revision is never pruned |
| `Version` | The profile version the revision was saved as |

A save that changes nothing records nothing. Saving a page without editing it,
or a deployment that finds nothing to add, does not create a revision. Otherwise
those writes would push real history out of the retention window. "Nothing" is
compared byte for byte on the stored encoding, so a switch saved as `true` where
it was `1` is a change.

### Where revisions are kept

- **Bodies** are objects in the AI stack's `ConfigurationBucket` at
  `config_revisions/<profile>/<nnnnnn>.json.gz`. Each object is a gzipped JSON
  envelope that holds the profile body and the revision's metadata. The bucket is
  encrypted with the stack's customer managed key and versioned. Its
  `DeletionPolicy` follows `EnableDataRetentionOnDelete`, as the table's does. When
  retention is off, the bucket is emptied and deleted with the stack.
- **The index** is one item per profile in `ConfigurationTable`,
  `ConfigRevIndex#<profile>`, holding a list of small entries. Listing a
  profile's history is one `GetItem` and never a scan. The index repeats what the
  envelopes hold, so it is a convenience over the objects rather than the only
  copy of anything.
- **The counters** `LatestRevision` and `PublishedRevision` sit on the profile
  record, so one read of a profile tells you which revision it currently is.

A profile name appears verbatim in the object key. That is safe because profile
names are restricted to `[A-Za-z0-9._-]` and may not start with `__`, so a name
can never hold a `/`.

### How a save records a revision

One rule holds the design together: **a profile's body is never replaced until
the revision it is has been recorded.** A save goes through three steps, in
this order:

1. If the record about to be replaced is not yet a recorded revision, the save
   records it first. If that fails, the save is refused with nothing written,
   and the page shows the error. This step handles two cases. One is a profile
   saved before history began, which is given its first revision number by a
   conditional write that changes only its counters and is then recorded as
   `backfill`. The other is a revision whose body did not land when it was
   saved; it is recorded from the record, which still holds that body, as
   `recovered`.
2. The save writes the profile, with the new revision's number allocated in that
   same conditional write. Only one write can succeed from a given record, so
   only one save can ever hold a given number. The number is one above both the
   record's `LatestRevision` and the highest number the index lists, so a number
   is not reused even where the counters have been lost, as on a record replaced by hand.
3. The save stores the new revision's body and index entry, then prunes. If
   this step fails, the save still stands and the failure is logged. The record
   now holds the body, so the next save records it, as `recovered`, before
   replacing it.

A body is only ever stored under a number that a committed write allocated. That
is what keeps two administrators who save at the same moment from storing two
different bodies under one number. On top of the save's own read and write, each
such save makes two `GetItem` calls on the index (one to plan, one to prune), one
`PutObject` and one `UpdateItem` to append. Once the retention cap is reached it
also makes a conditional `UpdateItem` to rewrite the index and one `DeleteObject`
per pruned revision.

A `recovered` revision has the author and time of the record it was recovered
from, which are those of the save that made it. What that revision changed and
its comment were in the object that did not land, so they are not known.

### Retention

Each profile keeps its **20** newest revisions. On top of those it always
keeps the revision that is its current state, every labelled revision, and every
pinned one. Nothing pins a revision yet; the flag is part of the rule so that
stamping a meeting with the revision it ran under can rely on it later. The cap
is a count, which an S3 lifecycle rule cannot express, so the application
enforces it after each save. A pruned entry leaves the index before its object
is deleted, so a reader never finds an entry with no body behind it.

A deleted or replaced object stays in the bucket as a non-current version for
**35 days**, which matches the table's point-in-time recovery window. A table
restored to any moment in that window therefore still finds the bodies its
index names. The bucket expires nothing else.

### Restoring, comparing and labelling

In the web UI this is the revision history dialog of the **View / Edit
Configuration** page, opened with **Revisions…** for the profile open, for Admin
users. The earlier Configuration History address, `#/configuration/history`, opens
that page with the dialog open. It lists the revisions with who saved each, when, what it changed,
how it came to be and its comment, and marks the current one. Select one
revision to **View** its whole configuration, **Label** it, or **Restore** it,
which asks for an optional comment first. A label left empty removes the label.
Select two to **Compare** them, setting by setting, from the older to the newer.
The configuration editor can also label the revision a save records, as part of
the save.

Underneath, these are GraphQL operations for the **Admin** group, pinned on the
AppSync field and checked again in the resolver Lambda behind it. Each takes a
`Profile`, which defaults to `custom` (the profile earlier releases created on
every deployment, which a deployment may no longer have); the Configuration
page passes the profile it shows.

| Operation | What it does |
|---|---|
| `Query.listConfigurationRevisions` | The retained revisions, newest first, plus the profile's current `Version` and counters. The revision that is the current state is always listed, even if its own save could not store it |
| `Query.getConfigurationRevision(Revision)` | One revision's metadata and its whole body, as a JSON string |
| `Query.diffConfigurationRevisions(FromRevision, ToRevision)` | Every value that differs between two revisions, as a JSON list of `{"Path", "Change", "Before", "After"}`. `Path` is a list of keys, because keys like `1#SUMMARY` hold separators of their own. Objects are compared key by key; a list or a string is compared as one value |
| `Mutation.restoreConfigurationRevision(Revision, ExpectedVersion, Comment)` | Saves an earlier revision's body as the profile's current one |
| `Mutation.labelConfigurationRevision(Revision, Label)` | Sets or clears a revision's label |

**Restore only moves forward.** The restored body is saved as a *new*
revision, with `Source: restore` and `RestoredFrom` naming the original. The
counter never rewinds, so the state the restore replaced stays in the history
and can itself be restored. A restore of the body the profile already holds
records nothing and reports `Unchanged`.

A restore puts back the **whole profile**, every section in it, because that
is what a revision is. That includes the sections other pages save. A restore
needs `ExpectedVersion`, the version `listConfigurationRevisions` returned. Like
any save, it is refused with a message to reload if the profile has changed
since. Readers pick up a restore as they pick up any save, which depends on each
section's tier (see [Three tiers](#three-tiers-declared-per-section)): at once
for `immediate`, within 30 seconds for `short`, and within five minutes for
`standard`.

### Upgrading an existing deployment: history starts at revision 1

Nothing to do. On an upgrade from a release with no configuration store, the
seeder creates the profile and carries every earlier store's edits into it in
one write (see [Carrying a later store across](#carrying-a-later-store-across)).
That write is **revision 1**, `Source: deploy`, by `cloudformation`, and it holds
the configuration exactly as the upgrade left it. A new deployment starts the
same way, with revision 1 holding the empty profile and empty sections the
seeder records. Either way the history has one revision, and the
`ConfigurationRevisionHistory` output reads `custom=present`. Later deployments
add a revision only when they add something to the profile.

There is one other case, kept for safety: a profile that exists but has no
revision history yet. That can only be a profile written by a pre-release build
of the configuration store, or saved by code that keeps no history. The seeder
records that profile as it stands as its first revision, with `Source: backfill`
and the author and time of whoever last saved it. Recording it does not count as
a save, so the profile's version, `UpdatedBy` and `UpdatedAt` stay as they were,
and a configuration page open at the time is not told that somebody saved it.
That run reports `custom=started`.

The stack output **`ConfigurationRevisionHistory`** says what the latest
deployment that ran the seeder found:

| Value | Meaning |
|---|---|
| `custom=present` | The profile's current state is a recorded revision. What a new deployment, an upgrade, and every later deployment report |
| `custom=started` | The profile existed with no history; its state as it stood has been recorded as its first revision |
| `custom=recovered` | The profile's current revision was not in the bucket, and has been recorded from the profile |
| `custom=saved-concurrently` | An administrator saved while the seeder ran; that save did the recording |
| `custom=not-started` | The history could not be written at deploy time. The deployment still succeeds and nothing is lost: the first save from a configuration page records the profile before replacing it, and is refused rather than made if it cannot. A section that the seeder could not add for the same reason is reported as `revision-history-unavailable` in `ConfigurationSectionAdoption`. **Check the seeder's log** (`/<stack>/lambda/ConfigurationSeederFunction`) for why |

**Rolling back** to a release with no configuration store removes
`ConfigurationBucket` along with `ConfigurationTable`, under the same
`EnableDataRetentionOnDelete` rule described in
[Upgrading an existing deployment: the summary prompts](#upgrading-an-existing-deployment-the-summary-prompts):
retained and orphaned when it is `true`, emptied and deleted when it is `false`.
The history goes with them. Deploying this release again creates a new table and
a new bucket, so history starts again at revision 1, holding the configuration
carried across from the per-store tables. If retention was on, the orphaned
bucket is where to read the earlier revisions from.

## Deploy-time seeding

A custom resource, `SeedConfiguration`, backed by
`lma-ai-stack/source/lambda_functions/configuration_seeder/`. It makes three
writes with different rules about what each may overwrite, and then carries
across the sections of stores that moved in after the profile existed:

- **`Config#default`** — written on create, and rewritten on every update on
  which the custom resource runs at all, which is every update that changes the
  shipped content or the code that writes it (see the content hash below), or
  one of its properties. It is this deployment's own content: the shipped
  defaults, then the preset the `ConfigurationPreset` stack parameter names,
  then the file `CustomConfigPath` names, if any. See
  [Choosing the default configuration at deploy time](#choosing-the-default-configuration-at-deploy-time).
- **One Managed profile per shipped preset** — `Config#<preset id>`,
  the preset applied over the baseline, rewritten on every run and recorded as a
  revision when its content changes. A preset is written only where the
  deployment can run it. A Managed profile for a preset a release no longer
  ships is deleted, unless it is the active profile: that includes the Managed
  profiles earlier 0.4.0 pre-releases wrote for presets this release does not
  ship, which stay while one is active and are deleted by the first update
  after another profile is activated. A Custom profile already holding a preset's
  name is left alone, and reported as `name-in-use`. Reported as the
  `ManagedProfiles` attribute.
- **`Config#custom`** — written only on a table that has never been seeded
  (no `Config#__active`): a new deployment, an upgrade from a release with no
  configuration store, or a table restored empty, so a stack update never
  discards an edit made from the web UI. The condition is on the records, not
  on `RequestType == 'Create'`, which would miss the restored table. A new
  deployment's profile holds nothing and is removed again (see
  [Named profiles](#named-profiles)), so the deployment runs on its default; a
  deployment that has run without it is not given it back.
- **`Config#__active`** — also written only if absent, so an administrator who
  selects a different profile keeps their choice.
- **One section of `Config#custom` per later store** — added only while the
  profile does not hold that section. See
  [Carrying a later store across](#carrying-a-later-store-across).
- **The revision history of `Config#custom`** — begun if it has not been, which
  happens once, on the upgrade that introduces it. The seeder is given the bucket
  as the `ConfigurationBucketName` property, and reports what it found as
  `ConfigurationRevisionHistory`. See
  [Upgrading an existing deployment](#upgrading-an-existing-deployment-history-starts-at-revision-1).
- **Summary sections a release no longer ships** — removed from each Custom
  profile that holds them unchanged. See
  [Summary sections a release no longer ships](#summary-sections-a-release-no-longer-ships).

### Choosing the default configuration at deploy time

Two stack parameters decide what `Config#default` holds, as IDP's
`ConfigurationPreset` and `CustomConfigPath` do:

- **`ConfigurationPreset`** — the id of a preset, applied over the shipped
  defaults: `standard` (the default, which is the shipped defaults),
  `internal-meetings-notify`, `regulated-consent-required` or
  `regulated-notify`; see
  [Configuration presets](configuration-guide.md#configuration-presets). `none`,
  the parameter's value in earlier 0.4.0 pre-releases, is also accepted and read
  as `standard`, so a stack deployed with it can be updated without choosing
  again. The three ids earlier pre-releases also offered
  (`healthcare-summary`, `translator-voice-assistant` and
  `on-demand-asr-with-voice-separation`) are no longer allowed values: a stack
  deployed with one of them has to set `ConfigurationPreset` to `standard` (or
  `none`) on its next update, which otherwise fails on the parameter value.
- **`CustomConfigPath`** — an `s3://bucket/key` of a JSON or YAML file laid over
  the result: an object of section name to settings, or a file exported from the
  Configuration page (`"format": "lma-configuration-profile"`), whose `sections`
  are used. Each section is checked against its schema, and a file that does not
  match, names a section the store does not have, or cannot be read fails the
  stack update, with the problems in the status reason. The seeder is granted
  `s3:GetObject` on that one object; a file encrypted with a customer-managed key
  also needs that key to allow the seeder's role. The file is read again on every
  stack update the seeder runs on, so it has to stay readable for the life of the
  stack, and an edit to it applies on the next update. To change it without an
  update, edit a Custom profile instead.

Both change only the baseline. A Custom profile keeps what it stores, and the
active profile stays active. The seeder reports what it applied as the
`ConfigurationPreset` and `CustomConfiguration` attributes.

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
every profile untouched, except to remove a
[summary section a release no longer ships](#summary-sections-a-release-no-longer-ships).

### Carrying a later store across

The summary prompts were adopted by *creating* the profile: when `Config#custom`
is absent, the seeder fills it from the previous table. When the seeder creates
the profile, it puts the later stores' settings into the creating write too.
That is what happens on a new deployment and on an upgrade from a release with no
configuration store, and it means the profile's first revision holds the whole
configuration as it arrived. A profile that already exists cannot be filled that
way, so on it each later store is carried across one **section** at a time:

- While the profile does not hold the section, the seeder reads the
  administrator's settings from the store's previous table and adds them to the
  profile as that section — an empty section if there was nothing to carry.
- Once the profile holds the section, nothing is read from the previous table
  again, whether the seeder put it there or an administrator saved it. An empty
  section counts, which is what makes a reset to defaults stick: a reset saves
  an empty section, and a later deployment does not bring the pre-upgrade values
  back.
- The write is conditional on the profile as the seeder read it, so an
  administrator saving any page during the deployment loses nothing; the
  seeder reads again and decides again.
- A previous table that cannot be read is not a failed deployment. The section
  is left unset, the shipped settings apply, and the next deployment that runs
  the seeder tries again. Unlike the profile's adoption this is not a one-shot,
  because nothing was written.

What each run found is the `SectionAdoption` attribute of the custom resource,
published as the **`ConfigurationSectionAdoption`** stack output: one
`section=outcome` pair per store, for example
`nova_sonic_config=adopted-from-previous-table`.

| Outcome | Meaning |
|---|---|
| `adopted-from-previous-table` | Settings were carried across, on this run |
| `nothing-to-adopt` | The previous table held no settings; an empty section was recorded |
| `already-present` | The profile already holds the section. Every run after the first reports this |
| `previous-table-unreadable` | **Act on this one**, or wait for the next deployment that runs the seeder: the settings were not carried across, and the shipped ones apply until they are. Saving the settings from their page also settles it |
| `no-previous-table` | The stack was given no previous table, so there is nothing to carry; nothing is recorded |
| `revision-history-unavailable` | The settings were read but not added, because the profile's current state could not be recorded in its [revision history](#revisions-and-audit) first, and a profile is not changed until it is. The deployment still succeeds, the section is left unset and the shipped settings apply, and the next deployment that runs the seeder tries again. `ConfigurationRevisionHistory` reads `custom=not-started` on the same run; check the seeder's log |

### Summary sections a release no longer ships

A Custom profile is a full copy, so it holds every summary section the baseline
shipped when it was made, and it is merged over the baseline when it is read. A
section a later release *adds* therefore reaches every profile through the
merge, with nothing for the seeder to do. A section a release *renames* does
not: the profile keeps the old key, the baseline brings the new one, and the
summary would run both.

The seeder keeps a list of the summary sections that are no longer shipped
(`RETIRED_SUMMARY_PROMPTS` in its handler), each with the section that replaces
it and a SHA-256 digest of every text a release shipped it with. The digest is
taken with `<br>` read as a line break and surrounding space ignored, so a
prompt saved with either form of line break still matches. On every run, after
`settle_profiles`, it looks at each Custom profile (not the baseline or a
Managed profile, which every run rewrites from the baseline):

- A retired section whose text matches a shipped digest was never changed, and
  is removed.
- A retired section set to `NONE` is removed, and the section replacing it is
  set to `NONE` in its place unless the profile already holds that section, so
  an administrator who turned the old section off does not find its replacement
  on. A profile copied from the healthcare preset, which turns off the sections
  it does not use, is such a profile.
- A retired section holding any other text was written by an administrator. It
  is kept, and keeps running beside its replacement; the seeder logs a warning
  naming the profile and the section on every run until an administrator
  removes one of them from the **Transcript Summary** section of the
  Configuration page.

The write is recorded as a revision by the deployment, conditional on the
profile as it was read: an administrator's save landing in between makes the
seeder read again and decide again. A run that finds nothing to remove writes
nothing, so the clean-up happens once. What each run found is the
`RetiredPrompts` attribute of the custom resource, `profile/key=outcome` pairs
such as `team/5#SLIDES PRESENTED=removed`, or `none`:

| Outcome | Meaning |
|---|---|
| `removed` | The section was removed: it held a shipped text, or was turned off and the profile already holds its replacement |
| `removed-successor-off` | The section was turned off; it was removed and its replacement turned off |
| `kept-changed` | The section holds an administrator's own prompt and was kept. Reported, and warned about, on every run |
| `deferred` | The profile could not be written now (its revision history could not be recorded, or it kept changing); the next deployment that runs the seeder tries again |

The one retired section today is **SLIDES PRESENTED**, replaced by **SCREEN
SHARE SUMMARY** in the same position (`5#`). Both were introduced during 0.4.0's
pre-releases, so only a deployment of one of those has a profile holding it.

## What is served from the store today

The **summary prompt templates** — the prompts behind each section of an
end-of-meeting summary, edited in the Transcript Summary section of the Configuration page. They were chosen
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

### Upgrading an existing deployment: the summary prompts

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
| `created-previous-table-unreadable` | **Act on this one.** Prompt templates customised before the upgrade have to be applied again from the Transcript Summary section of the Configuration page. The previous table is retained, so the old values can be read from it. |
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
to be applied again from the Transcript Summary section of the Configuration page**; if retention was on, the
orphaned table is where to read the old values from.

### The Nova Sonic voice assistant settings

The settings in the **Nova Sonic Voice Assistant** section of the Configuration page — the system prompt
and prompt mode, model, voice, endpointing sensitivity, meeting mode, and the
translator languages and trigger phrases — are the `nova_sonic_config` section.

- `Config#default` holds the shipped settings, from
  `configuration_seeder/defaults/NovaSonicConfig.json`. The Nova Sonic stack
  still ships its own copy for its own table, and a test fails if the two
  differ.
- `Config#custom` holds the settings an administrator changed, and only those: a
  setting put back to its default is not stored, so the shipped value applies.
  **Reset section to defaults** stores an empty section.
- The Virtual Participant reads the section when its voice assistant starts —
  the ElevenLabs provider reads the meeting mode and translator settings from it
  too — on the `immediate` tier: a consistent read and no cache, three `GetItem`
  calls per meeting. It combines the shipped and administrator layers exactly as
  it combined the previous table's `DefaultNovaSonicConfig` and
  `CustomNovaSonicConfig` items.
- It falls back to the previous table (`NovaSonicConfigTable`) only when the
  configuration table cannot be read or holds no shipped Nova Sonic settings —
  a deployment rolled back across the move, say. That table is still seeded by
  its own stack, and the administrator's settings in it are the ones saved
  before the upgrade.
- `Query.getNovaSonicConfig` and `Mutation.updateNovaSonicConfig` are both served
  by one Lambda resolver, for the same reason as the summary prompts. The read
  keeps both of its authorization modes — the **Admin** group on Cognito
  user-pool authentication, and IAM — and still answers with a JSON string; what
  is in it is now the section's settings plus `Version`, `UpdatedBy` and
  `UpdatedAt`, rather than the whole DynamoDB item, which also carried the
  partition key, the seeded guidance text and a description. A save that sends
  `ExpectedVersion` is refused if the profile has changed since, as for the
  summary prompts.

**On an upgrade** the seeder carries the settings saved on the page across into
`Config#custom` the first time it runs, as described in
[Carrying a later store across](#carrying-a-later-store-across); check the
`ConfigurationSectionAdoption` output afterwards. Editing `CustomNovaSonicConfig`
in the DynamoDB console stops having any effect, as for the summary prompts:
edit the settings on the Configuration page.

**One consequence of one shared profile.** The version a save is checked against
is the profile's, and every section is saved into the same profile — from the
Configuration page, the Edit Chat Buttons dialog or the API. So a save is also
refused when another administrator saved a *different* section after this one
was loaded. Nothing is lost either way — the Configuration page offers to reload
and keep the changes — but it is a refusal that a per-section record would not
have produced.

### The Meeting Assist chat buttons

The quick-action buttons in the Meeting Assist chat panel are the
`chat_buttons` section, keyed `N#Label` where `N` orders them.

- `Config#default` holds the shipped buttons, from
  `configuration_seeder/defaults/ChatButtonConfig.json`, numbered as the
  chat-button-config stack numbers its own copy. A test fails if the two files
  differ.
- `Config#custom` holds the buttons an administrator saved from the **Edit Chat
  Buttons** dialog, as the whole list. This section is the one exception to
  "a profile is a delta": a non-empty custom list *replaces* the shipped buttons
  rather than overriding them key by key, so nothing in it is pruned against
  the baseline, and the two records are read separately rather than merged.
  **Reset to Defaults** stores an empty list, and the shipped buttons apply.
- `Query.getChatButtonConfig` and `Mutation.updateChatButtonConfig` are served
  by one Lambda resolver. The read stays open to every signed-in user and to
  IAM, because the chat panel renders its buttons for whoever is viewing a
  meeting, and it answers with one record's buttons and its `Version` —
  never the attributes saying who saved it. The write stays Admin-only and is
  conditional on the version the dialog loaded.

**On an upgrade** the seeder carries the custom buttons across as described in
[Carrying a later store across](#carrying-a-later-store-across); the
`ConfigurationSectionAdoption` output reports `chat_buttons=...`. Editing the
`CustomChatButtonConfig` item in the DynamoDB console stops having any effect:
edit the buttons from the dialog.

### The ASR engine switches

The three switches in the **Transcription Engine** section of the Configuration page — which engine streaming
meetings use, which engine Virtual Participants use, and whether a Virtual
Participant asks for per-voice labels — are the `asr_engine` section.

- `Config#default` holds them as the release ships them, from
  `configuration_seeder/defaults/AsrEngine.json`: both engine switches on, so
  streaming meetings and Virtual Participants use the ASR MicroVM engine, and
  voice separation off. On a deployment without that engine the readers find no
  ASR launcher configured and use Amazon Transcribe whatever the switches say.
  The previous table had no seeder, and its readers took an absent record as
  every switch off; that reading now applies only to a rolled-back stack.
- The active profile holds the switches an administrator saved, which win over
  the baseline, so a release that changes the shipped switches reaches only the
  deployments whose administrator has not saved them.
- The WebSocket transcriber reads the section on the `short` tier. It falls back
  to the previous `AsrConfigTable` only when the configuration table cannot be
  read or holds no shipped switches, and a fallback answer is reused for the same
  30 seconds, so a table that cannot be read does not add a failed read to the
  start of every meeting.
- `Query.getAsrConfig` and `Mutation.updateAsrConfig` are served by one Lambda
  resolver, created on every deployment: the Virtual Participant reads its voice
  separation switch through it, and that switch applies on Amazon Transcribe too.
  On a deployment without the on-demand engine the Configuration page shows only
  that switch in the section, since the engine choices have nothing to select. The
  read stays open to every signed-in user and to IAM — the Stream Audio and
  Upload Audio forms read it, and the Virtual Participant reads its two switches
  with SigV4 — and answers with the *effective* switches plus `Version`. The
  write stays Admin-only and is conditional on the version the page loaded.

**On an upgrade** the seeder carries the saved switches across as described in
[Carrying a later store across](#carrying-a-later-store-across), reporting
`asr_engine=...` in `ConfigurationSectionAdoption` — `no-previous-table` on a
deployment without the on-demand engine. Turning the engine on later passes the
new table's name to the seeder, which then records the (empty) section.
### The meeting cost price table

The unit prices the [meeting cost estimate](meeting-cost-estimate.md) is computed
with, and the source of each, are the `call_pricing` section. `Config#default`
holds the shipped table, from `configuration_seeder/defaults/CallPricing.json`, so
a release that updates a price reaches the baseline on its next stack update. An
Admin changes prices in the section's price table on the Configuration page, or
through `updateCallPricing`; both save into the active profile, with the same
version check. `getCallPricing` reads the shipped table, what the active profile
stores and the merge of the two. The estimate reads the section's baseline values
beside its merged ones (`ResolvedSection.baseline_values`), which is how each line
says whether its price is the release's or the profile's. There was no earlier
store for prices, so nothing is adopted on upgrade.

## Settings that were stack parameters until 0.4.0

Until 0.4.0, 24 released stack parameters only ever reached application code -- a model
id, a number, a switch passed to a function as an environment variable. 0.4.0
removes them as a clean break and makes each a Configuration page setting with a
shipped default. Three settings
new in this release (the knowledge-base and key frame text models, and the agenda
timing) are listed there too, though no release had them as parameters.

- `lma_config/setting_defaults.json` holds each setting's shipped default and
  type, by section and setting, and, for each one that was a stack
  parameter in a release, the parameter it replaced
  (`formerParameter`). The seeder writes the defaults into `Config#default`,
  under the content of the other default files and the `ConfigurationPreset` and
  `CustomConfigPath` stack parameters.
- `read_setting(section, setting)` is how a Python reader gets a value: the
  active configuration; then, for the two settings a stack parameter still backs
  (retention), the parameter's environment variable; then the caller's `default`;
  then the shipped default. So a reader that cannot reach the table still has a
  value. `lma-ai-stack/test/test_parameter_backed_settings.py` holds every call
  to a listed setting, every reader's own fallback to the shipped default, and
  the readers' table access.
- The Virtual Participant (`stack-settings.ts`, which applies the
  `virtual_participant`, `transcription` and `recording` sections to its process
  environment as it starts) and the WebSocket transcriber
  (`calleventdata/session-settings.ts`, which reads them as each meeting starts)
  read the configuration first. Their nested templates keep their own
  parameters of the same names, which the main stack no longer passes, so each
  task's environment holds the nested template's default as its fallback.
- The reaper is always deployed and scheduled; **Meeting Processing › Minutes
  without speech before a meeting is ended** set to 0 makes each run end nothing.
- The meeting assistant's wake phrase falls back to the shipped pattern when the
  one saved on the page does not compile.

**On upgrade**, the values a deployment's parameters held are not carried
across: the deployment uses the shipped defaults, or what an Admin saved on the
page. See
[Settings that were stack parameters before 0.4.0](configuration-guide.md#settings-that-were-stack-parameters-before-040)
for what to do instead. `lma deploy` drops the removed parameters from an update
on its own; a pipeline that passes them has to stop.

## Stack parameters as upper limits: retention

`MeetingRecordExpirationInDays` and `TranscriptionExpirationInDays` set when a
record expires: each writer stamps `ExpiresAfter`, the DynamoDB TTL, as the record
is written. For retention an administrator may want to keep records for less
time than the deployment allows, but a deployment's retention is often a
compliance commitment, so a runtime value must not lengthen it. These two are
therefore settings of a `retention` section (standard tier) whose parameter is
the **upper limit** as well as the default:

- The seeder writes the parameters into the baseline as for any other
  parameter-backed setting, and the schema marks both settings
  `maximumIsDefault`. A save above the baseline's value is refused (see
  [The validator](#the-validator)), and the page shows the limit beside each.
- `read_setting` answers a limited setting with the smaller of the section's
  value and the parameter's environment variable, so the parameter bounds what
  is used even when the section holds more: a restored revision, or a value
  saved before a stack update lowered the parameter. A section value that is not
  above zero is ignored. The TypeScript half's `limitedSetting` does the same.
- A change applies to records written after it, within the standard tier's five
  minutes. A record keeps the expiry it was written with.
- The recordings bucket's lifecycle rules stay tied to the parameters:
  `TranscriptionExpirationInDays` also deletes transcript files and key-frame
  images in the bucket, and that half did not move.

The writers, each of which carries the parameter's variable as the limit:

| Records | Writer | How it reads the section |
|---|---|---|
| Meeting records and summaries; transcript segments and meeting assistant answers | Call event processor, the transcript summary and agent-assist orchestrators | `get_meeting_ttl` and `get_transcription_ttl` in `eventprocessor_utils`, through `read_setting`, with `MEETING_RECORD_EXPIRATION_IN_DAYS` and `TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS` |
| Usage (metering) records | The six AI-stack functions that write them | `lma_metering.records`, through `read_setting`, with `METERING_EXPIRATION_DAYS` |
| Usage records | The meeting assistant | `call_metering.py`, through `configuration_settings.read_setting` |
| Usage records | The WebSocket transcriber | Read with each meeting's settings (`session-settings.ts`) and applied to the records written after it |
| Usage records | The Virtual Participant | Applied to `METERING_EXPIRATION_DAYS` as it starts (`stack-settings.ts`), never raising it |

`lma-ai-stack/test/test_parameter_backed_settings.py` finds every AI-stack
function that writes such records and holds it to carrying the variable from the
right parameter, the table's name, the layer and read access to the table. The
two orchestrators did not carry the variables before this section existed, so
they gave their records 90 days whatever the parameters said; they now follow
both.

## Stack parameters that stay

Of the parameters that only application code reads, these two are kept as
stack parameters by design (the others not yet on the Configuration page are
listed under [Still to come](#still-to-come)):

- `LogLevel` is read by each process's logging when the process starts, so a
  runtime value would apply at an unpredictable later time.
- `BedrockGuardrailVersion` chooses which version of the deployment's Bedrock
  guardrail applies. The guardrail is a safety control, so changing which version
  applies stays a deployment.

`TranscribeContentRedactionType` stays because it has one allowed value, `PII`,
so there is nothing to choose; whether redaction applies is the **Redact
personal information** switch in the Amazon Transcribe section.

`VoiceEnrollmentRetentionDays` and `VoiceEnrollmentTermsVersion` stay too, for
the reason given in [Voice Enrollment](voice-enrollment.md): they are part of
the consent terms each enrollment agrees to.

## Still to come

| Stage | What it adds |
|---|---|
| The configuration editor's JSON and YAML views, and comparing profiles | The page edits one profile at a time, through its form |
| Moving the troubleshooting agent's prompt | Its `Default`/`Custom` pair in `LLMPromptTemplateTable` becomes a section here; then that table can be retired. It waited for the configuration editor: the custom prompt is edited in the DynamoDB console today, which is exactly the route a compressed body closes, so moving it before there was an editor would have left it with no way to edit it. With the editor in place, it needs a schema and a seeder adoption |
| Replacing the SSM settings blob | The `<stack>-LMASettings` parameter is written only by CloudFormation, at deploy time, and read by the web app for every signed-in user. It is a deployment's wiring rather than something an administrator edits. The Configuration page reads the configuration at run time, but every other page reads only this blob, so a setting another page needs moves when that page reads the configuration too |

A shipped summary section that has been turned off is listed by the
Configuration page, marked **Turned off**, with **Turn on**, which brings back
that one section and nothing else.

One of the six stores this page's opening counts is not migrated at all.
**The MCP server list** (`MCPServersTable`) is a registry of installed servers,
one record each, holding each server's credentials and updated in place as its
OAuth tokens are refreshed. That is entity data with its own lifecycle rather
than a configuration document, and putting credentials into a profile body would
mix them into a record every configuration page reads. It stays a table of its
own; the `standard` tier is its caching policy, generalised.

The troubleshooting agent's prompt is not one of the six — it lives in the table
the summary prompts moved out of — and it does move, for the reason given in its
row of the table above.

## Reference

| What | Where |
|---|---|
| Python accessor | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/` |
| TypeScript accessor | `lma-websocket-transcriber-stack/source/app/src/config/configuration-store.ts` (edit this one) and its copy `lma-virtual-participant-stack/backend/src/configuration-store.ts` |
| The Virtual Participant's Nova Sonic reader | `lma-virtual-participant-stack/backend/src/nova-sonic-config-loader.ts` |
| Revision history | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/revisions.py`, driven from `store.py` |
| Section schemas, and the validator that applies them | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/schemas/` and `schema.py`; the shared validation fixture `lma-ai-stack/test/fixtures/configuration_schema_cases.json` |
| Editor and history resolver (read and save the configuration; list, read, compare, restore, label revisions) | `lma-ai-stack/source/lambda_functions/configuration_revisions_resolver/` |
| The configuration library | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/presets/` and `presets.py` |
| The configuration editor in the web UI | `lma-ai-stack/source/ui/src/components/configuration/`: `ConfigurationPage.jsx`, the controls in `fields.jsx`, the rules in `config-model.js` and the validator copy in `schema-validation.js` |
| Deploy-time seeder | `lma-ai-stack/source/lambda_functions/configuration_seeder/` |
| Retention limits | `maximumIsDefault` in `schemas/retention.json`, `limit_problems` in `schema.py`, applied by the store's `write_sections`; `limitedSetting` in the TypeScript accessor |
| Stack parameters that are defaults, and the reader for them | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/parameter_defaults.json` and `parameters.py`; in TypeScript `parameterBackedSetting` in the shared accessor and the transcriber's `calleventdata/session-settings.ts`; the cross-template test `lma-ai-stack/test/test_parameter_backed_settings.py` |
| Table, bucket, layer, custom resource and the `ConfigurationRevisionWriteAccess` policy every writer attaches | `lma-ai-stack/deployment/lma-ai-stack.yaml` |
| Content-hash computation | `lib/lma_sdk/lma_sdk/_core/publish.py` |
| Cross-language contract test | `lma-ai-stack/test/test_configuration_store_contract.py` |
