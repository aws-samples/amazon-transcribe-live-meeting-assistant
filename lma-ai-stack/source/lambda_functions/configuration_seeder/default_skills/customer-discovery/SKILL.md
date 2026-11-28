---
name: customer-discovery
display_name: Customer discovery
description: Playbook for a customer discovery call - how the customer works today, their pain points and requirements, the competitors and alternatives they named, budget and timing, and the next steps with owners on both sides. Use when asked what the customer needs, how we compare with what they evaluated, or what was agreed as next steps.
playbook:
  suggestFor:
    - customer discovery
    - discovery call
    - discovery session
    - customer call
    - customer interview
    - prospect call
    - intro call
  whiteboards:
    - template: blank
      title: Customer call notes
      instructions: Keep the Notes panel under these headings, in the customer's own terms - Pain points, Requirements (must-have or nice-to-have only when the customer said which), Objections and concerns, Budget and timing, Next steps (each with its owner on either side and its date). Write "none mentioned" under a heading the call gave nothing for.
      assistantUpdates: true
    - template: comparison
      title: Requirements and alternatives
      instructions: One row per requirement the customer stated. Columns - Requirement, Must-have or nice-to-have, How they do it today, then one column for each competitor or alternative they named, with what was said about how it meets the requirement. Leave a cell empty rather than guessing.
      assistantUpdates: true
  guidance:
    checks:
      - lookup
      - ask_question
      - uncovered_topic
      - action_missing_owner
      - action_missing_due_date
    assertiveness: balanced
    maxItemsPer10Min: 5
  quickActions:
    - label: Customer's needs
      prompt: Summarise the customer's pain points and requirements so far, in their own words, each with the transcript time.
    - label: Questions still to ask
      prompt: Which discovery topics have we not covered yet - current process, pain points, decision makers, budget, timeline, alternatives evaluated? Suggest one question for each.
    - label: Next steps and owners
      prompt: List the next steps agreed so far with the owner on each side and the date, and flag any without an owner or a date.
---

# Customer discovery

This meeting runs the **Customer discovery** playbook. The people running it want to come away knowing how the customer works today, what hurts and what it costs them, what a solution must do, what else they have looked at, their budget and timing, and who does what next.

1. Read the call with `current_meeting_transcript` (mode `full`, with `with_timestamps` on), which also holds the meeting chat, and its agenda with `get_meeting_agenda`. When something was shown on screen, read it with `get_meeting_frames`.
2. Tell the customer's side from the seller's side by the speakers' names and what they say. Report the customer's statements in their own terms, and do not present the seller's pitch as a customer requirement.
3. Mark a requirement as must-have or nice-to-have only when the customer said which.
4. For a competitor, product or term the customer named, and only when `document_search` or `web_search` is among your tools, you may look it up to explain what it is; say where the explanation came from, and keep it apart from what the customer said about it.
5. Next steps need an owner on either side and a date. List those that have neither, so they can be settled before the call ends.
6. When the meeting's whiteboards can be read (`list_meeting_whiteboards`), the **Customer call notes** and **Requirements and alternatives** whiteboards hold the call as it stands; read them before answering about the customer.
