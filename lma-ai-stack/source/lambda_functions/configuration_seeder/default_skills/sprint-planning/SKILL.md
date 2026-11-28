---
name: sprint-planning
display_name: Sprint planning
description: Playbook for a sprint or iteration planning meeting - the sprint goal, the stories committed with estimates, owners, dates and dependencies, the scope against capacity, and the risks. Use when asked about the sprint plan, the stories, who owns what, what is in or out of scope, or whether the sprint fits the team's capacity.
playbook:
  suggestFor:
    - sprint planning
    - iteration planning
    - sprint kickoff
    - backlog planning
    - PI planning
  whiteboards:
    - template: user_stories
      title: Sprint backlog
      instructions: One story per item the team commits to this sprint, with its estimate in points, its owner and its ticket id when said. Put a story the team discussed but left out of the sprint under Open questions as "Not in this sprint", with the reason.
      assistantUpdates: true
    - template: gantt
      title: Sprint plan
      instructions: One task per committed story, in a section per owner, with the start and end dates the team said. A story that waits on another starts after it.
      assistantUpdates: true
    - template: risks
      title: Risks and dependencies
      instructions: Capacity, dependencies on other teams or people, and anything said to put the sprint goal at risk.
      assistantUpdates: true
  guidance:
    checks:
      - action_missing_owner
      - action_missing_due_date
      - clarify
      - risk_probe
      - uncovered_topic
      - time_check
    assertiveness: balanced
    maxItemsPer10Min: 6
  quickActions:
    - label: Scope vs capacity
      prompt: Add up the estimates of the stories committed so far and compare them with the capacity the team gave. Say which stories are over the line if it does not fit.
    - label: Stories without owners
      prompt: List the stories discussed so far that have no owner, no estimate or no dates yet.
    - label: Sprint summary
      prompt: Write the sprint plan so far - the goal, each committed story with its owner, estimate and dates, the dependencies and the risks - ready to paste into the team channel.
---

# Sprint planning

This meeting runs the **Sprint planning** playbook. The people running it want, by the end, a sprint goal, a list of committed stories that fits the team's capacity, an owner and dates for each, the dependencies between them, and the risks.

1. Read the meeting with `current_meeting_transcript` (mode `full`, with `with_timestamps` on), which also holds the meeting chat, and its agenda with `get_meeting_agenda`. The meeting context usually gives the sprint dates and the capacity.
2. Treat a story as **committed** only when the team agreed to take it into the sprint. A story that was discussed and pushed out is "not in this sprint"; say so rather than listing it as committed.
3. Use estimates, owners and dates only as they were said. Never invent a date or an estimate. Where one is missing, say it is missing; that is what the team needs to hear.
4. When asked whether the sprint fits, add up the committed estimates and compare them with the capacity the meeting gave. When no capacity was given, say so and give the total.
5. When the meeting's whiteboards can be read (`list_meeting_whiteboards`), the **Sprint backlog**, **Sprint plan** and **Risks and dependencies** whiteboards hold the plan as it stands; read them before answering about the plan, and say when the transcript has moved on since.
6. Keep answers short and in the team's own words. A ticket id (such as `ORB-305`) is more useful than a paraphrase of the story.
