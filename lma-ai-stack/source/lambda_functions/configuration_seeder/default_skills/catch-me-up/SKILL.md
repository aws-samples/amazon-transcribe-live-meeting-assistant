---
name: catch-me-up
display_name: Catch me up
description: Catch up someone who joined the current meeting late or stepped away, with what happened since a given time or since they joined, then the topic being discussed now. Use when asked to "catch me up", "what did I miss" or "what happened since ...".
---

# Catch me up

Adapted from the Amazon Quick skill `lma-meeting-catchup` for a meeting that is still running.

1. Work out the starting point. Use the time the request gives ("since 10:15", "the last ten minutes"). If it says "since I joined", look in the transcript for when the person asking first spoke or was greeted. If neither tells you, cover the whole meeting and say so in one line.
2. Read the meeting with `current_meeting_transcript` (mode `full`, with `with_timestamps` on), which also holds the meeting chat, and keep what was said and typed after the starting point. Use `get_meeting_agenda` to name the agenda items covered in that time.
3. If a slide or a page was on the shared screen in that time and it matters to what was said, read it with `get_meeting_frames` (with `start_time`) and mention it in one bullet.
4. Answer in this shape:
   - **Since <time>**: three to six bullets, in order, each one thing that was said, decided or asked. Name who said it where it matters.
   - **Decisions and actions**: any that were agreed in that time, with the owner; leave this out if there were none.
   - **Now**: one or two sentences on the topic being discussed at the end of the transcript.

Keep it short enough to read in under a minute. Do not repeat what happened before the starting point.
