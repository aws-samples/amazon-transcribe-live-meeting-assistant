---
name: architecture-review
display_name: Architecture review
description: Playbook for an architecture or design review - the components and how they connect, the design decisions with their reasons and the alternatives rejected, the trade-offs, the open questions and the risks. Use when asked about the proposed design, why an option was chosen, what is still undecided, or what could go wrong.
playbook:
  suggestFor:
    - architecture review
    - design review
    - technical design
    - design doc review
    - system design
    - architecture
  whiteboards:
    - template: architecture
      title: Architecture
      instructions: The design being proposed, not the one being replaced, unless the meeting compares them. Mark a component that is still under discussion with "(proposed)".
      assistantUpdates: true
    - template: decision_log
      title: Decision log
      instructions: One record per design decision the meeting reached, with the alternatives it considered and why each was rejected. A decision reversed later in the meeting is marked Superseded.
      assistantUpdates: true
    - template: decisions
      title: Open questions and follow-ups
      instructions: Keep Decisions to the one-line form of each design decision. Under Open questions, what the review left undecided and who is to answer it.
      assistantUpdates: true
  guidance:
    checks:
      - risk_probe
      - clarify
      - fact_check
      - ask_question
      - action_missing_owner
    assertiveness: balanced
    maxItemsPer10Min: 6
  quickActions:
    - label: Trade-offs so far
      prompt: Compare the options discussed so far in a table - one row per option, with what speaks for it, what speaks against it, and whether it was chosen or rejected.
    - label: Unanswered questions
      prompt: List the design questions raised in this review that have not been answered, with who raised each one and who is expected to answer it.
    - label: Draft decision record
      prompt: Write a decision record (status, context, decision, consequences, alternatives considered) for the main decision this review reached, from what was said.
---

# Architecture review

This meeting runs the **Architecture review** playbook. The people running it want, by the end, a clear picture of the proposed design, each decision with its reason and the alternatives rejected, the questions left open with owners, and the risks.

1. Read the meeting with `current_meeting_transcript` (mode `full`, with `with_timestamps` on) and its agenda with `get_meeting_agenda`. When a diagram or a document was shared on screen, read what was shown with `get_meeting_frames`.
2. Tell a **decision** (the reviewers agreed) from a **proposal** (someone suggested it) and from an **assumption** (taken for granted, not discussed). Name each for what it is.
3. For each decision, give the reason that was said and the alternatives that were rejected, with why. When no reason was given, say so: an unexplained decision is a finding of the review.
4. When a question about a product's limit, price or behaviour matters to a decision and `document_search` or `web_search` is among your tools, check it there and cite what you found; otherwise say it was not checked.
5. When the meeting's whiteboards can be read (`list_meeting_whiteboards`), the **Architecture**, **Decision log** and **Open questions and follow-ups** whiteboards hold the review as it stands; read them before answering about the design.
6. Do not add components, protocols or technologies that were not mentioned.
