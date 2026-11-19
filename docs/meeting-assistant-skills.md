---
title: "Meeting Assistant Skills"
---

# Meeting Assistant Skills

## Table of Contents

- [Overview](#overview)
- [What a Skill Holds](#what-a-skill-holds)
- [The Skills Page](#the-skills-page)
- [Importing and Exporting a Skill](#importing-and-exporting-a-skill)
  - [Agent Skills: SKILL.md and a skill folder](#agent-skills-skillmd-and-a-skill-folder)
  - [Amazon Quick skill files](#amazon-quick-skill-files)
  - [What an import leaves out](#what-an-import-leaves-out)
- [The Skill Library](#the-skill-library)
- [Choosing Skills in the Profile](#choosing-skills-in-the-profile)
- [How the Assistant Uses a Skill](#how-the-assistant-uses-a-skill)
- [SDK and CLI](#sdk-and-cli)
- [Who Can Read and Change Skills](#who-can-read-and-change-skills)
- [Limits](#limits)
- [Related Documentation](#related-documentation)

## Overview

A **skill** is guidance an administrator writes for the meeting assistant about one kind of request: how to run a launch review, what to record during an incident, how to answer questions about an internal procedure. Each skill has a name, a description of when it applies, Markdown instructions, and optional text reference files.

The assistant reads skills by **progressive disclosure**. Its system prompt lists only the name and description of each skill the request may use. When a request matches a description, the assistant calls `load_skill` to read that skill's instructions, and `read_skill_file` to read a reference file the instructions name. A request that needs no skill pays only for the short list, and a skill's text reaches the model only when it is used.

Skills are stored in their own table (`<stack>-Skills`), account-wide, as MCP servers are. Which skills the assistant may use is set in the configuration profile, per skill, under **Meeting Assistant › Tool selection** on the Configuration page.

## What a Skill Holds

| Part | Stored as | Rules |
|---|---|---|
| Id | `SkillId` | Set when the skill is created, from its name (with a number added if that id is taken), and never changed. The profile names skills by id. |
| Name | `Name` | Lowercase letters, digits and single hyphens between them, up to 64 characters, as the Agent Skills format defines a name. Unique within the deployment. The assistant loads a skill by its name. |
| Display name | `DisplayName` | Optional, up to 128 characters. Shown on the pages. |
| Description | `Description` | Required, up to 1,024 characters. The assistant decides from it whether a request needs the skill, so say what the skill is for and when to use it. |
| Instructions | `Instructions` | Required Markdown, up to 48 KB. |
| Reference files | `Files` | Up to 20 text files, each up to 32 KB, with a relative path such as `reference/checklist.md`. The types are `.md`, `.markdown`, `.txt`, `.csv`, `.json`, `.yaml` and `.yml`. |
| Metadata | `Metadata` | The front-matter fields kept from an imported file: `trigger`, `icon`, `inputs`, `license`, `compatibility` and the Agent Skills `metadata` map. Saved unchanged when the skill is edited, and written back on export. Not sent to the assistant. |
| Source | `SourceFormat` | Where the skill came from: `manual`, `agent-skills`, `amazon-quick` or `library`. |
| Audit | `CreatedAt`, `CreatedBy`, `UpdatedAt`, `UpdatedBy` | The user's email address, or the IAM principal's ARN for an SDK or CLI call. |

The instructions and reference files together are at most 64 KB. A skill holds instructions and text only. Nothing in a skill is run: there is no script, no code and no tool definition in it, and a skill cannot give the assistant a tool it is not otherwise offered.

Every signed-in user can read the skills (see [Who Can Read and Change Skills](#who-can-read-and-change-skills)), so do not put credentials or other secrets in a skill.

## The Skills Page

**Configuration › Skills** in the navigation, beside **MCP Servers**, lists every skill with its description, its number of reference files, its trigger (for a skill imported from Amazon Quick), its source and who last changed it. Select a skill to act on it:

- **Export skill**: download it as `SKILL.md`, or as a `.zip` of its folder with its reference files.
- **Edit** (Admin): the editor has the name, display name and description, the instructions with a **Write** tab and a **Preview** tab that renders the Markdown, and the reference files, each with its path and its text. **Add file** adds an empty one and **Add from disk** reads a text file into a new one. Metadata kept from an import is shown and saved unchanged.
- **Delete** (Admin). A profile that names the skill keeps its entry, which then has no effect; the Configuration page shows it as **No such skill** with **Remove**.

**Create skill** opens the same editor for a new skill. **Import / export** offers importing one skill, importing a library, and exporting the library. **When importing a skill, replace a skill of the same name** decides what an import of a skill whose name is taken does: without it the import is refused and nothing changes; with it, the stored skill is replaced and keeps its id, so a profile that names it still does.

## Importing and Exporting a Skill

One skill is imported from one file, which the stack reads (`importSkill`). The import reports what it created or replaced, each file it left out with the reason, and notes such as a name it changed to fit the naming rule.

### Agent Skills: SKILL.md and a skill folder

The [Agent Skills](https://agentskills.io) format is a folder holding a `SKILL.md` and, optionally, other files. `SKILL.md` starts with YAML front matter giving at least `name` and `description`, followed by the instructions:

```markdown
---
name: launch-review
description: Review a product launch plan against the launch checklist. Use when asked to review a launch or a launch plan.
license: MIT-0
metadata:
  author: product-team
---

# Launch review

1. Read the checklist in reference/checklist.md.
2. Compare the plan discussed in the meeting with each item, and list what is missing.
```

- **A `SKILL.md` on its own** imports the skill without reference files.
- **A `.zip` of the folder** imports `SKILL.md` and the files beside it, at their paths in the folder. `SKILL.md` can be at the root of the zip or inside its one top-level folder. Zips of up to 512 KB are read.

A name that does not fit the naming rule (`Launch Review`, say) is stored in the nearest form that does (`launch-review`), and the import says so. The `allowed-tools` field is not kept, because the tools the assistant may use are chosen in the configuration profile; the import says so. Other front-matter fields are not kept and are listed.

Export writes the same formats: **Export as SKILL.md** writes the front matter and the instructions, and **Export as .zip** writes a folder named for the skill with `SKILL.md` and every reference file.

### Amazon Quick skill files

An [Amazon Quick](amazon-quick-mcp-setup.md) skill file, such as those in `amazon-quick-desktop-skills-pack/skills/`, is a Markdown file with front matter giving `name`, `display_name`, `trigger`, `icon` and `inputs`, and no description:

```markdown
---
name: lma-meeting-catchup
display_name: LMA Meeting Catch-up
trigger: catch me up on
icon: 📋
inputs:
  - meeting_description
---

# LMA Meeting Catch-up

Quickly catch up on meetings you missed with a synthesized summary combining LMA data with Slack context.
...
```

It is recognised by those fields and the absent description. The description is taken from the first paragraph of the body, with the trigger phrase added (`... Trigger phrase: "catch me up on".`), and `display_name` becomes the display name. `trigger`, `icon` and `inputs` are kept as metadata and written back when the skill is exported as `SKILL.md`. Instructions written for Amazon Quick may name tools the meeting assistant does not have (Outlook or Slack, for example); edit the instructions to the tools the meeting assistant has.

### What an import leaves out

A skill holds instructions and text reference files only, so an import of a zip leaves out, and lists with the reason:

- a file whose type is not one of the text types above: scripts (`.py`, `.sh`, `.js` and the rest), programs, images and other binary files;
- a file marked executable in the zip;
- a file that starts with an interpreter line (`#!`);
- a file that is not UTF-8 text;
- a hidden or system file (a name starting with `.`, or `__MACOSX/`).

An import is refused, and stores nothing, when a reference file is larger than 32 KB, when the skill is larger than the limits above, when the file has no front matter or no description, or when the zip holds no `SKILL.md` or more than one skill folder. Creating or editing a skill with a reference file of another type is refused in the same way.

## The Skill Library

**Export the skill library** downloads every skill as one JSON document, and **Import a skill library** reads one back, on this deployment or another:

```json
{
  "kind": "lma-skill-library",
  "version": 1,
  "exportedAt": "2026-11-19T12:00:00Z",
  "skills": [
    {
      "skillId": "launch-review",
      "name": "launch-review",
      "displayName": "Launch review",
      "description": "Review a product launch plan against the launch checklist.",
      "instructions": "# Launch review\n\n1. Read the checklist ...",
      "files": [{ "path": "reference/checklist.md", "content": "- Pricing\n- Support\n" }],
      "metadata": { "license": "MIT-0" }
    }
  ]
}
```

`kind` and `version` name the document's type and format version, as the [MCP server library](mcp-servers.md#exporting-and-importing-the-server-library)'s do.

**An import matches skills by name.** A skill whose name is stored replaces the stored one and keeps its id. Any other skill is created, under the id the file gives when this deployment has no skill of that id, so that a configuration profile exported with the library still names it; otherwise under an id made from its name. An entry that cannot be read (no name, a name that does not fit the rule, no description) is skipped with the reason, and the others are still imported. A reference file that is not text is left out of its skill and listed. An entry that matches its stored skill exactly is not written, so re-importing a deployment's own export changes nothing, not even who last changed a skill. The result lists each skill as `created`, `updated`, `unchanged`, `skipped` or `failed`.

A library of up to 40 skills is read. The pages and the SDK send a large library in parts of whole skills, keeping only the first entry of a name listed more than once (and saying so), which an import by name then makes the same as sending it whole.

## Choosing Skills in the Profile

**Meeting Assistant › Tool selection** on the Configuration page lists the skills beside the installed MCP servers, with the same three settings for each, stored in the active profile under `meeting_assist.toolSelection.skills`, keyed by skill id:

```json
{
  "mcpServers": {},
  "skills": {
    "launch-review": { "inMeetingRequests": true },
    "incident-notes": { "enabled": false }
  }
}
```

| Setting | Stored as | Effect |
|---|---|---|
| **Enabled** | `enabled` | Off: the assistant does not use the skill at all, for the owner's requests or for in-meeting ones. On, or not set, is the default. |
| **In-meeting requests** | `inMeetingRequests` | **Allowed** (`true`) or **Not allowed** (`false`) for in-meeting requests. Not set: the In-meeting requests setting decides, as for every skill: under **Selected tools**, the **In-meeting requests: skills** switch (`inMeetingSkills`); under **Unrestricted**, allowed. |
| **Always on** | `alwaysOn` | Reserved for a later always-on mode. It is stored and validated, and nothing reads it yet. |

How a request is offered skills, as for [MCP servers](meeting-assistant.md#choosing-mcp-servers-one-by-one):

- **The meeting page's chat** (the owner's own requests) is offered every skill that is not turned off.
- **Current meeting only** offers no skill, whatever a skill's own setting.
- **Selected tools** offers a skill when its own **In-meeting requests** is Allowed, or when it is not set and the **In-meeting requests: skills** switch is on.
- **Unrestricted** offers every skill not turned off, except one set to Not allowed.

A skill the profile does not name is offered as the defaults say, so a profile with no skill entries offers every skill to the owner's requests and none to in-meeting requests under the shipped In-meeting requests setting. The assistant is told, by name, which skills are not available for a request (those turned off or not allowed), so that it says so when a request needs one. A profile entry for a skill that is not stored has no effect.

The selection is in the profile, and so is versioned, compared, exported and imported with the rest of it; the skills themselves move with the skill library.

## How the Assistant Uses a Skill

For each request, the meeting assistant function reads the list of skills (cached for five minutes, as the MCP server list is), keeps those the request may use, and builds its system prompt as:

1. the profile's editable **Assistant instructions**;
2. the list of the allowed skills, each by name with its description, inside `<available_skills>`, with the instruction to call `load_skill(name)` when a request matches a description and to read a reference file with `read_skill_file(name, path)` when the instructions say to;
3. the fixed part LMA appends, which no profile can change (see [Assistant Instructions](meeting-assistant.md#assistant-instructions)), ending with one more fixed rule when skills are offered: what `load_skill` and `read_skill_file` return is an administrator's guidance, the one tool result the assistant may follow as instructions, and only for the request it applies to and within the fixed rules. A skill cannot change or set aside a fixed rule, cannot give the assistant a tool it was not offered, and cannot make anything another tool returns into instructions; where a skill and the fixed rules disagree, the fixed rules apply;
4. the notes for the request (the meeting chat, the in-meeting scope, the tools and skills that are not available).

The list comes before the fixed part, so the fixed part's opening line, that it applies whatever the instructions above it say, covers the list too.

`load_skill` returns the skill's instructions inside `<skill_instructions>`, marked as written by an LMA administrator, with the paths of its reference files; `read_skill_file` returns one file inside `<skill_file>`. Both answer only for the skills the request may use: a skill the profile does not allow for the request cannot be loaded by naming it. A skill's instructions and files are cached by the skill's update time, so an edit is read once the five-minute list refresh sees it.

The function logs, for each request, `Skills: offered=<ids> not offered=<ids>`, and each `load_skill` and `read_skill_file` call by skill id; the meeting page shows each call as a thinking step, as for every tool.

## SDK and CLI

The LMA SDK's `client.skills` and the CLI's `lma skills` do what the page does, signed with the caller's AWS credentials:

```python
from pathlib import Path
from lma_sdk import LMAClient

client = LMAClient(stack_name="LMA")
client.skills.create(
    "launch-review",
    "Review a product launch plan against the launch checklist.",
    Path("instructions.md").read_text(),
    files={"reference/checklist.md": Path("checklist.md").read_text()},
)
client.skills.update("launch-review", description="Review a launch plan.")
client.skills.import_file("lma-meeting-catchup.md")           # SKILL.md, Quick .md or .zip
client.skills.export("launch-review", "launch-review.zip", file_format="zip")
library = client.skills.export_library()
LMAClient(stack_name="LMA-other").skills.import_library(library)
```

```bash
lma skills list
lma skills get launch-review
lma skills create --name launch-review --description "Review a launch plan." \
    --instructions-file instructions.md --file reference/checklist.md=checklist.md
lma skills update launch-review --description "Review a product launch plan."
lma skills delete launch-review --yes
lma skills import launch-review.zip --replace
lma skills export launch-review --format zip
lma skills export-library --output skills.json
lma --stack-name LMA-other skills import-library skills.json
```

`update` keeps every part it is not given; `--file` on `lma skills update` replaces the whole list of reference files.

## Who Can Read and Change Skills

- **Reading** (`listSkills`, `getSkill`, `exportSkill`) is open to every signed-in user and to IAM-signed callers, as the installed MCP server list is. The skills shape the answers every user of the meeting assistant gets, the meeting page shows each `load_skill` call, and a skill holds no credential, so there is nothing in a skill that a user of the assistant should not be able to read.
- **Changing** (`createSkill`, `updateSkill`, `deleteSkill`, `importSkill`, `importSkillLibrary`) and **exporting the whole library** (`exportSkillLibrary`) are for the Admin group and for IAM-signed callers, which is how the SDK and CLI call AppSync; an IAM caller is a principal with `appsync:GraphQL` permission on the deployment's API. The Skills Manager function checks the Admin group again for a user-pool caller. The roles the stacks create for their own functions and for the Virtual Participant, some of which may call every other mutation, are denied the skill writes explicitly, as they are the MCP server library import.
- The Skills page is in the navigation's **Configuration** section, which only Admins see, as the MCP Servers page is.

## Limits

| What | Limit |
|---|---|
| Skills per deployment | 40 |
| Instructions | 48 KB |
| Reference files per skill | 20 |
| One reference file | 32 KB |
| Instructions and reference files together | 64 KB |
| Description | 1,024 characters |
| An imported zip | 512 KB |
| Entries in one library document | 40 |

Sizes are bytes of UTF-8. The limits keep each skill within one DynamoDB item and the whole library within one AppSync response.

## Related Documentation

- [Meeting Assistant](meeting-assistant.md): the built-in tools, in-meeting requests, and the assistant's instructions
- [MCP Servers](mcp-servers.md): the other kind of item the profile's tool selection names, and its library
- [Configuration Guide](configuration-guide.md) and [Configuration Store](configuration-store.md): the Meeting Assistant section and the profile
- [LMA CLI and SDK](lma-cli.md)
