---
name: decisions-and-actions
display_name: Decisions and actions
description: List the current meeting's decisions and action items as a table, with owners, due dates and blockers, and flag the actions with no owner or no due date. Use when asked for the decisions, the action items, the next steps or who owns what.
---

# Decisions and actions

1. Read the meeting with `current_meeting_transcript` (mode `full`, with `with_timestamps` on), which also holds the meeting chat.
2. Find each **decision**: something the meeting agreed, chose or ruled out. A proposal nobody agreed to is not a decision.
3. Find each **action**: something a person or team is to do after the meeting. Record its owner and due date only as they were said; record a blocker when the meeting named something the action is waiting for.
4. Answer with two tables:

   | # | Decision | Time |
   |---|---|---|

   | # | Action | Owner | Due | Blocker | Time |
   |---|---|---|---|---|---|

   Time is the transcript time (`MM:SS`) where it was agreed. Leave a cell empty rather than guessing.
5. Under the tables, list the actions with **no owner** and those with **no due date**, each by its number, so they can be settled before the meeting ends. Say so plainly when there are none.

When the request comes from the meeting chat, keep the tables short: the action and its owner, then the flags.
