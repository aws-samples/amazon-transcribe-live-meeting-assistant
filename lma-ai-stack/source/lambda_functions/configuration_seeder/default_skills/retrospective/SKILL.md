---
name: retrospective
display_name: Retrospective
description: Playbook for a team retrospective - what went well, what to improve, and the actions the team agreed with their owners and dates. Use when asked what came up in the retrospective, which themes repeat, or which actions were agreed.
playbook:
  suggestFor:
    - retrospective
    - sprint retrospective
    - retro
    - lessons learned
    - project retrospective
  whiteboards:
    - template: retrospective
      title: Retrospective
      instructions: Merge points that repeat and note how many people raised each. An action belongs under Actions only once the team agreed to it.
      assistantUpdates: true
  guidance:
    checks:
      - action_missing_owner
      - action_missing_due_date
      - uncovered_topic
      - time_check
    assertiveness: quiet
    maxItemsPer10Min: 4
  quickActions:
    - label: Themes so far
      prompt: Group what has been said so far into a few themes, under went well and to improve, with how many people raised each.
    - label: Actions and owners
      prompt: List the actions the team agreed so far with their owners and dates, and flag any without an owner or a date.
---

# Retrospective

This meeting runs the **Retrospective** playbook. The people running it want, by the end, what went well, what to improve, and a short list of actions the team agreed, each with an owner and a date.

1. Read the meeting with `current_meeting_transcript` (mode `full`), which also holds the meeting chat, and its agenda with `get_meeting_agenda`.
2. Group points that say the same thing, and say how many people raised each; a point several people raised matters more than one raised once.
3. Keep it about the work, not the people: report what happened and what made it hard, not who did something badly.
4. An idea becomes an **action** only when the team agreed to do it. List the actions that have no owner or no date, so they can be settled before the meeting ends.
5. When the meeting's whiteboards can be read (`list_meeting_whiteboards`), the **Retrospective** whiteboard holds the retrospective as it stands; read it before answering.
