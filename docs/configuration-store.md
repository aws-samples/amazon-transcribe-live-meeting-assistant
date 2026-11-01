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
earlier store, and the Amazon Bedrock model choices, whose defaults are the stack
parameters that used to be the only way to set them; see
[Stack parameters as defaults](#stack-parameters-as-defaults). Every save into the store records a revision — who saved it,
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
| `Config#<name>` | A profile: an administrator's edits, stored as a *delta* over the baseline. Today there is one, `Config#custom`. |
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
LatestRevision  PublishedRevision
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
| `language_models` | `standard` | Read once per summary, answer, agenda pass, naming pass, knowledge-base query or key frame; nobody is waiting on a model change, and the summariser's **Regenerate** drops the cache before reading, as for the prompts |

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
| `language_models` | The four model ids, each one of the models its stack parameter allows, nothing else |
| `call_pricing` | Prices that are finite and not negative, under `regions › <region or default> › <service>/<api-or-model> › <unit>`; region codes in the form `us-east-1`; the lists and notes as lists and objects of text; `currency` `USD`; no keys the estimator does not read |

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
into a failure.

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
| `Query.getConfiguration(Profile)` | Everything an editor needs in one read: `Sections`, a JSON list of each section's `Name`, `CacheTier` and `Schema`; `Default`, the shipped baseline; `Custom`, the profile's stored delta; and the profile's `Version`, `UpdatedBy`, `UpdatedAt` and `PublishedRevision`. Read consistently from the stored records, never from a cache. Only registered sections are returned |
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

The Configuration page's **Import, export and presets** menu moves configuration
between deployments, and applies named presets. None of the three saves anything
by itself: an import or a preset becomes unsaved changes on the page, marked and
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
- **Presets** are the configuration library: named, shipped changes, each
  `lma_config/presets/<id>.json` in the configuration layer, served by
  `getConfiguration` as `Presets`. A preset names the sections it changes, and
  for each one how: `merge` lays its values over the section as it stands, so the
  settings it names change and every other one stays; `replace` makes the section
  exactly its values, with every shipped entry it does not name turned off where
  the section can turn one off. A setting the deployment does not offer is left
  out. The layer's tests hold every preset to that shape and its values to the
  section schemas, and the healthcare preset to the prompts the healthcare domain
  ships.

| Preset | What it changes |
|---|---|
| Healthcare summary | Replaces the summary sections with the healthcare domain's — summary, details, and SOAP and BIRP notes — turning the other shipped sections off |
| Translator voice assistant | Sets the Nova Sonic meeting mode to Translator, English and Spanish, with the shipped trigger phrases |
| On-demand speech engine with voice separation | Moves streaming meetings and Virtual Participants onto the on-demand engine, and turns on Virtual Participant voice separation. Offered only where the engine is deployed |

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

In the web UI this is the **Configuration ▸ Configuration History** page, for
Admin users. It lists the revisions with who saved each, when, what it changed,
how it came to be and its comment, and marks the current one. Select one
revision to **View** its whole configuration, **Label** it, or **Restore** it,
which asks for an optional comment first. A label left empty removes the label.
Select two to **Compare** them, setting by setting, from the older to the newer.
The configuration editor can also label the revision a save records, as part of
the save.

Underneath, these are GraphQL operations for the **Admin** group, pinned on the
AppSync field and checked again in the resolver Lambda behind it. Each takes a
`Profile`, which defaults to `custom`, the profile every configuration page
saves into.

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
  shipped content or the code that writes it (see the content hash below). It is
  this deployment's own content.
- **`Config#custom`** — written **only if it is not already there**, so a stack
  update never discards an edit made from the web UI. The condition is on the
  record, not on `RequestType == 'Create'`: keying off the request type would
  also skip the case where the record is genuinely absent on an update, which is
  what happens after a table restore or a hand deletion.
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

- `Config#default` holds them as a new deployment ships them, all off, from
  `configuration_seeder/defaults/AsrEngine.json`. The previous table had no
  seeder and read an absent record as exactly that.
- `Config#custom` holds the switches an administrator saved.
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
with are the `call_pricing` section. `Config#default` holds the shipped table, from
`configuration_seeder/defaults/CallPricing.json`, so a release that updates a price
reaches every deployment on its next stack update. An Admin's price changes are a
delta in `Config#custom`, written through `updateCallPricing` and read back, with
the shipped table and the merge of the two, through `getCallPricing`. There was no
earlier store for prices, so nothing is adopted on upgrade, and the shared-profile
version check above applies to an `updateCallPricing` save as it does to the
configuration pages' saves.

## Stack parameters as defaults

Some stack parameters only ever reached application code: a model id passed to a
function as an environment variable and read there. Such a setting moves into a
section of this store, and its parameter stays, as the setting's **default**.
Removing the parameter would break every deploy command and pipeline that
passes it, so it is kept; what changes is what it means.

- `lma_config/parameter_defaults.json` names, for each such parameter, the
  section and setting it is the default of, and the setting's type (`string`,
  `integer` or `boolean`). `lma_config/parameters.py` reads it.
- The AI stack passes the parameters' values to the seeder as the
  `ParameterDefaults` property of `SeedConfiguration`, keyed by the
  `lma-main.yaml` parameter name. The seeder writes each into `Config#default`,
  after reading it back into its type and checking it against the section's
  schema; a value that fails either is left out and logged. Because the values
  are properties of the custom resource, a stack update that changes one re-runs
  the seeder, which is what makes the baseline follow the parameter. The
  custom resource reports which it wrote as its `ParameterDefaults` attribute.
- The values go into the **baseline**, never into a profile. The baseline is
  what the deployment was deployed with; the profile is what an administrator
  changed. So an administrator's value wins until it is reset, and a later
  change to the parameter reaches every deployment where nobody has changed the
  setting.
- A reader calls `read_setting(section, setting, env_var=...)` where it uses the
  value, not at import, so the section's tier decides how soon a change is
  seen. It prefers the resolved section; falls back to the parameter's
  environment variable, which the function keeps, when the section does not
  hold the setting or the table cannot be read; and then to its own default. A
  function that already holds a store passes it, so the read shares that
  store's cache. `read_setting` refuses a setting that no parameter backs.
- The section's schema lists, for a setting with a fixed set of choices, exactly
  the parameter's `AllowedValues`, so the page offers what a stack update could
  set.

On upgrade there is nothing to do and nothing to adopt: the parameters' values
are the deployment's current settings, and the first deployment that includes a
moved parameter writes its value into the baseline. Until that deployment's
seeder has run, the readers use their environment variable, which holds the same
value.

`lma-ai-stack/test/test_parameter_backed_settings.py` holds the three halves
together: every listed parameter is passed from `lma-main.yaml` to the AI stack
and from there to the seeder; its default and every allowed value are values
the section accepts, and a listed choice is exactly its allowed values; and
every `read_setting` call names a listed setting, on a function whose fallback
environment variable is set from the same parameter and which has the table,
the layer and read access to it.

### The Amazon Bedrock models

The `language_models` section holds the four Bedrock model ids that were each a
stack parameter: `summaryModelId` (`BedrockModelId`), read by the summariser and
the functions that read invitations, name speakers, derive agendas, query the
transcript knowledge base and serve the MCP search tool; `meetingAssistModelId`
(`MeetingAssistServiceBedrockModelID`), read by the meeting assistant and the
troubleshooting agent; `knowledgeBaseModelId`
(`MeetingAssistKnowledgeBaseModelID`), read by the meeting assistant's
knowledge-base tools; and `keyframeTextModelId` (`KeyframeTextModelId`). Every
one of those readers' Bedrock permissions covers all foundation models and
inference profiles, so a model chosen here never needs a permission the stack
lacks. The meeting assistant and the troubleshooting agent are in the
meeting-assist stack, which is given the table's name and the layer's ARN as the
AI stack's `ConfigurationTableName` and `ConfigurationPythonLayerArn` outputs. That
template can still be deployed on its own, given neither: its functions then read
the model parameters' environment variables, as they did before
(`lma-meetingassist-setup-stack/src/configuration_settings.py`).

The summariser reads the model once per summary, after its prompt templates, so
every section of one summary is written by the same model. The troubleshooting
agent reads it as each run starts.

## Still to come

| Stage | What it adds |
|---|---|
| Named profiles | More than one profile, a way to choose the one in force, and an import that can create one. Export, import and presets work today on the one `custom` profile; see [Export, import and the configuration library](#export-import-and-the-configuration-library) |
| Moving the troubleshooting agent's prompt | Its `Default`/`Custom` pair in `LLMPromptTemplateTable` becomes a section here; then that table can be retired. It waited for the configuration editor: the custom prompt is edited in the DynamoDB console today, which is exactly the route a compressed body closes, so moving it before there was an editor would have left it with no way to edit it. With the editor in place, it needs a schema and a seeder adoption |
| Moving more CloudFormation parameters | The other parameters that are read only by application code and do not gate a resource, a condition or an IAM policy, in the way described under [Stack parameters as defaults](#stack-parameters-as-defaults) |
| Replacing the SSM settings blob | The `<stack>-LMASettings` parameter is written only by CloudFormation, at deploy time, and read by the web app for every signed-in user. It is a deployment's wiring rather than something an administrator edits, so it moves with the parameters above, when the web app reads its configuration at run time |

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
| Stack parameters that are defaults, and the reader for them | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/parameter_defaults.json` and `parameters.py`; the cross-template test `lma-ai-stack/test/test_parameter_backed_settings.py` |
| Table, bucket, layer, custom resource and the `ConfigurationRevisionWriteAccess` policy every writer attaches | `lma-ai-stack/deployment/lma-ai-stack.yaml` |
| Content-hash computation | `lib/lma_sdk/lma_sdk/_core/publish.py` |
| Cross-language contract test | `lma-ai-stack/test/test_configuration_store_contract.py` |
