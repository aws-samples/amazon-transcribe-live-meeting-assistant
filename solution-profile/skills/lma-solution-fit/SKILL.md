---
name: lma-solution-fit
description: Assess whether the Live Meeting Assistant (LMA) fits a meeting transcription, summary or meeting-assistant need, using its solution profile. Use when someone describes meetings they need to capture, transcribe, summarize or get AI help in, and wants to know whether LMA fits, what to ask, and what would have to be built around it.
---

# Assess fit for the Live Meeting Assistant

This skill works from LMA's **solution profile**, a set of YAML files published with
each release. The profile files sit two directories above this file: this skill is
`skills/lma-solution-fit/SKILL.md` inside the profile directory, beside
`solution-profile.yaml`.

Read the profile of the release you are assessing, not a newer or older one.
`solution-profile.yaml` says which release it describes (`version`) and which release
a person last reviewed it against (`reviewed_for_version`).

## 1. Read the profile

| File | Use it for |
|---|---|
| `solution-profile.yaml` | What LMA is, its support model, and `signals`: keywords and phrases that suggest a need it serves, and `anti_signals` that suggest it does not |
| `capabilities.yaml` | What it does: the capture methods, the meeting platforms the Virtual Participant joins (tagged `vp-platform:`), the speech engines, the assistant, the meeting record, consent and privacy, the hosted MCP server's tools (tagged `mcp-tool:`) and the configuration presets (tagged `preset:`), each with its status |
| `limitations.yaml` | Where it stops, with numbers, how much each limit matters (`impact`), the questions that detect it (`detect_with`) and what mitigates it |
| `extension-points.yaml` | Every documented way to change its behaviour, with a gap category, effort and upgrade risk |
| `discovery-questions.yaml` | The questions that decide fit and configuration, and what each answer implies |
| `deployment.yaml` | How and where it deploys, and the prerequisites |
| `sizing.yaml` | The inputs that size a deployment and LMA's own cost estimators |
| `delivery.yaml` | How a delivery usually starts, and typical work items with effort |
| `changes.yaml` | What each release changed, and what an upgrade requires someone to act on |

Capabilities, extension points, limitations and deployment options carry
`evidence`: a path in LMA's repository at the release commit, or a documentation
URL. Cite evidence when you state a fact, and prefer the evidence over your own memory
of the product.

Items are cited as `<kind>:<id>`, for example `limitation:nova-sonic-concurrent-streams`.
Use those citations in your notes so a person can check each one.

## 2. Recognise the need

Compare what you are told with `signals` and `anti_signals` in the manifest. A need
is a likely match when people meet online (Zoom, Microsoft Teams, Google Meet, Webex
or Amazon Chime, or any meeting a participant's computer can hear) and want a live
transcript, summaries and action items, an assistant during the meeting, a record of
consent, or meeting content made available to other systems and AI assistants, in an
AWS account they operate. It is likely not a match when the core need is telephony
contact-center analytics, a meeting platform of its own, or a hosted service the
customer does not operate. If you are unsure, say so and ask; do not stretch the
match.

## 3. Ask the questions

Work through `discovery-questions.yaml` by `priority` (`must`, then `should`, then
`could`) and by `phase`. `pre-contract` questions decide fit; `discovery` and `build`
questions shape the configuration. Ask each question as written: they are phrased to
be asked directly. Record each answer with the question id.

For each answer, read the question's `implications` and follow its `tests` to the
capabilities and limitations it bears on. A limitation whose `detect_with` names the
question is the one to check first. Follow `follow_ups` when an answer opens one.

Three answers decide most assessments: the meeting platforms and their guest and bot
policies (`question:meeting-platforms`, `question:bot-policy`), the consent and
retention policy (`question:consent-policy`, `question:retention-policy`), and the
Region (`question:data-residency`). Do not assume an answer you were not given. Where
an answer is missing, list the question as open rather than guessing.

## 4. Classify each gap

For every requirement LMA does not meet as it stands, choose the **most preferred**
category that closes it. The order of preference is:

1. **configuration**: settings on the Configuration page, configuration profiles and
   presets, prompt templates, summary profiles, skills, knowledge bases and external
   MCP servers. No code. The deployment's operator owns it.
2. **hook**: customer-owned code at a documented hook point that LMA calls, such as
   the per-segment transcript Lambda hook or the end-of-call summary function. The
   customer owns it.
3. **extension**: something built beside LMA on its integration points: rules on the
   meeting events bus, a consumer of the meeting export, an MCP client, a custom
   capture client, an embedded view. The customer or a partner owns it.
4. **core-enhancement**: a change to LMA itself, contributed upstream. It ships when
   the maintainers release it, so its timing is not the customer's to decide.
5. **fork**: a private copy with code changes. Least preferred: every upgrade then
   has to be merged by hand, and `changes.yaml` shows how often releases arrive.
6. **other**: work outside LMA, with an `other_kind` of `aws-service`, `integration`,
   `data`, `process`, `security-review` or `infrastructure`; for LMA this often means
   meeting platform settings, AWS quotas, or a legal review of recording consent.

Use the extension points in `extension-points.yaml` as the menu. Name the extension
point you would use, its `effort` and `upgrade_risk`, and its `constraints`.

If no category closes a requirement, not even a fork, the verdict for that
requirement is **not-a-fit**. Say which requirement it is and why. One not-a-fit
requirement that is essential makes LMA a poor fit overall, however well the rest
matches.

## 5. Report

Give a short assessment a person can check:

- the release the profile describes;
- the match, in one or two sentences, citing capabilities;
- each limitation that applies, with its numbers and the answer that triggered it;
- each gap, with its category, the extension point, effort and upgrade risk;
- any not-a-fit requirement;
- the open questions;
- the support model: the customer deploys, owns and operates LMA, and is responsible
  for complying with the laws and policies on recording the meetings it captures.

State what you inferred separately from what the profile says, never present a
`preview` or `experimental` capability as generally available, and never recommend a
`deprecated` or `retired` capability for new work.
