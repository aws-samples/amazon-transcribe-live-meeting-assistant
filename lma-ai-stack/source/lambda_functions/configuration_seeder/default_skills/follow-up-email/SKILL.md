---
name: follow-up-email
display_name: Follow-up email
description: Draft a follow-up email for the current meeting, with a short summary, the decisions, the action items with their owners and due dates, and the open questions. Use when asked to write, draft or send a follow-up, recap or thank-you email after a meeting.
---

# Follow-up email

1. Read the meeting with `current_meeting_transcript` (mode `full`), which also holds the messages typed in the meeting chat. Read its agenda with `get_meeting_agenda`, and use the agenda's wording for the topics where it has any.
2. If something was shown on the shared screen that the email should mention, read it with `get_meeting_frames`. If files were shared in the meeting chat, list them with `get_chat_attachments` and name the ones the email should refer to.
3. Write the email:
   - **Subject**: the meeting's purpose and date, in one line.
   - **Greeting**, then two or three sentences on what the meeting was for and where it ended.
   - **Decisions**: one bullet each, as decided, not as discussed.
   - **Action items**: one bullet each, as `Owner — action — due date`. Write "owner to be confirmed" or "date to be confirmed" where the meeting did not set one; never invent either.
   - **Open questions**: what was raised and not answered, with who is to answer it when that was said.
   - A one-line close.
4. Leave out small talk and anything said off the topic of the meeting. Quote nobody at length.

Ask for the recipients or the tone only when the request needs them and the meeting does not make them clear; otherwise write to "everyone who attended" in a neutral, professional tone, and say in one line after the draft that both can be changed. Do not send the email: return the draft.
