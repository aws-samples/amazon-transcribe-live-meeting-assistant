---
name: next-meeting-prep
display_name: Next meeting prep
description: Draft an agenda for the next meeting from the current meeting's open items, and from past meetings with the same people when past meetings can be searched. Use when asked to prepare, plan or draft the agenda for the next or follow-up meeting.
---

# Next meeting prep

1. Read the current meeting with `current_meeting_transcript` (mode `full`) and its agenda with `get_meeting_agenda`. Collect the **open items**: agenda items not covered, questions not answered, actions due before the next meeting, and anything someone said should be "picked up next time".
2. If `recent_meetings_list` and `meeting_history` are among your tools, look for earlier meetings with the same people or on the same subject: list recent meetings with `recent_meetings_list`, then search the ones that match with `meeting_history` (passing their `call_ids`) for items left open there. Use only what they return.
3. If those two tools are not available for this request, draft the agenda from the current meeting alone, and say in one line that past meetings were not searched because they are not available for this request.
4. Answer with:
   - **Proposed agenda**: numbered items, each with a one-line purpose, the person expected to lead it when that is clear, and a suggested length in minutes. Start with a review of the actions due, and end with next steps.
   - **Carried over from earlier meetings**: the items from step 2, each naming the meeting it came from; leave this out when none were searched or none were found.
   - **To prepare**: what each person was asked to bring or do before the next meeting.

Do not invent a date, a time or attendees for the next meeting.
