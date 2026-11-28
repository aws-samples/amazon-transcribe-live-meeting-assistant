---
name: incident-review
display_name: Incident review
description: Playbook for a blameless incident review or post-incident review - the timeline with clock times, how the incident was detected and mitigated, the root cause and contributing factors, and the follow-up actions with owners and due dates. Use when asked for the incident timeline, what caused it, what made it worse, or the actions agreed.
playbook:
  suggestFor:
    - incident review
    - post incident review
    - post incident
    - incident retrospective
    - postmortem
    - post mortem
    - outage review
    - correction of error
    - incident
  whiteboards:
    - template: timeline
      title: Incident timeline
      instructions: Each event with its clock time and time zone as said - first impact, detection, each escalation, each mitigation step, recovery and all clear. Put an event whose time is not known yet under Notes with "time to be confirmed".
      assistantUpdates: true
    - template: decisions
      title: Contributing factors
      instructions: Use Decisions for the root cause and each contributing factor the review agreed on (technical, process or detection), one bullet each, without naming anyone as at fault. Open questions holds what the review could not yet explain.
      assistantUpdates: true
    - template: blank
      title: Follow-up actions
      instructions: Keep the Notes panel as one Markdown table with the columns Action, Owner, Due, and Reduces (the contributing factor it addresses), one row per follow-up action the review agreed. Leave Owner or Due empty when it was not said, and list those actions under the table as still needing an owner or a date.
      assistantUpdates: true
  guidance:
    checks:
      - action_missing_owner
      - action_missing_due_date
      - clarify
      - uncovered_topic
      - ask_question
    assertiveness: balanced
    maxItemsPer10Min: 6
  quickActions:
    - label: Timeline so far
      prompt: Write the incident timeline so far as a table of clock time, event and who acted, from what was said in this review.
    - label: Gaps in the timeline
      prompt: Which parts of the incident have no times yet, or which steps between detection and recovery has the review not explained?
    - label: Actions and owners
      prompt: List the follow-up actions agreed so far with their owners and due dates, and flag any without an owner or a date.
---

# Incident review

This meeting runs the **Incident review** playbook, for a blameless review. The people running it want, by the end, an accurate timeline, how the incident was detected and mitigated, the root cause and the contributing factors, and follow-up actions that each have an owner and a due date.

1. Read the review with `current_meeting_transcript` (mode `full`, with `with_timestamps` on), which also holds the meeting chat, and its agenda with `get_meeting_agenda`. When a dashboard, a graph or a log was shown on screen, read it with `get_meeting_frames`.
2. Keep it **blameless**: describe what happened and what the systems and processes allowed, never who was at fault. When a request asks who caused the incident, answer with the conditions that led to it.
3. Give clock times with the time zone as they were said. Never guess a time; say which events have no time yet.
4. Tell the **root cause** from **contributing factors** (what made it worse, slower to detect or slower to mitigate) and from **triggers** (what set it off), as the review named them.
5. An action needs an owner and a due date. List the actions that have neither, so they can be settled before the review ends.
6. When the meeting's whiteboards can be read (`list_meeting_whiteboards`), the **Incident timeline**, **Contributing factors** and **Follow-up actions** whiteboards hold the review as it stands; read them before answering about the incident.
