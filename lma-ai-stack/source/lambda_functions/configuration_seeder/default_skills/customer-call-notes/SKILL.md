---
name: customer-call-notes
display_name: Customer call notes
description: Write notes on a customer call - the customer's pain points, requirements, objections, the competitors mentioned and the next steps. Use when asked for customer call notes, a call summary for the account team, or what the customer needs.
---

# Customer call notes

1. Read the call with `current_meeting_transcript` (mode `full`, with `with_timestamps` on), which also holds the meeting chat. Read the meeting's agenda with `get_meeting_agenda`, and, if the call had a demo or slides, what was shown with `get_meeting_frames`.
2. Tell the customer's side from the seller's side by the speakers' names and what they say. Where that is unclear, say which statements you could not attribute.
3. Write the notes under these headings, each a short list in the customer's own terms, with the transcript time (`MM:SS`) of each point:
   - **Pain points**: the problems the customer described, and what they cost them where they said.
   - **Requirements**: what a solution must do, with any numbers, dates or limits given. Mark each as must-have or nice-to-have only when the customer said which.
   - **Objections and concerns**: price, risk, timing, security, effort, or anything else they pushed back on, and how it was answered if it was.
   - **Competitors and alternatives**: each product, vendor or in-house option named, and what was said about it.
   - **Next steps**: each with its owner on either side and its date, as agreed.
4. Write "none mentioned" under a heading the call gave nothing for. Do not add anything the call did not say.
