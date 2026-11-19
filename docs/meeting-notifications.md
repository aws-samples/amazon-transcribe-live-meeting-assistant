---
title: "Meeting Notifications"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Notifications

A **meeting notification** is a short item LMA shows, live, to the people who
can see a meeting: its owner, the users it is shared with, and Admins. It is
shown in LMA only. It is never posted to the meeting's chat, never spoken by a
voice assistant, never sent by email, and never visible to the meeting's other
participants.

Every notification has a **category**. `guidance` is guidance for the people
running the meeting, such as a note an AI assistant leaves through the hosted
MCP server's `push_meeting_guidance` tool. Other features can publish
notifications of their own categories through the same mechanism.

Nothing is published until a feature or an MCP client publishes something, and
a meeting with no notifications looks as it always has.

## Contents

- [On the meeting page](#on-the-meeting-page)
- [Who sees them](#who-sees-them)
- [Dismissing and muting](#dismissing-and-muting)
- [Buttons on a notification](#buttons-on-a-notification)
- [Retention](#retention)
- [Reading them over MCP](#reading-them-over-mcp)
- [Publishing a notification from a backend function](#publishing-a-notification-from-a-backend-function)
- [How it is stored](#how-it-is-stored)
- [Cost](#cost)
- [Limitations](#limitations)

## On the meeting page

Once a meeting has a notification, its page shows a **Notifications** drawer
beside the help panel, with a bell icon. The drawer can be resized by dragging
its edge, and closed to its icon. Its width is remembered in the browser.

- The drawer lists the meeting's notifications **in the order they were first
  published**, both those published before the page was opened and those that
  arrive while it is open.
- Each notification shows its severity (**Info**, **Suggestion**, **Warning** or
  **Urgent**), its category and type, the time, its title, its body (Markdown),
  its buttons, and who published it.
- A notification that a feature updates (see [Deduplication](#deduplication))
  is replaced where it is, and marked **updated**.
- A notification published with an expiry time leaves the page when that time
  passes. It is still stored, and still returned over MCP.
- When a notification arrives while the page is open, the drawer opens by
  itself. If you have closed it on this meeting during this visit, it stays
  closed and its icon shows a badge instead.
- The drawer's controls filter it by category, show dismissed notifications
  again, and mute a category (see below).

Bodies are rendered as Markdown without raw HTML and without pictures, so a
notification never makes the browser load anything from an address in its text.
Links open in a new tab.

A meeting shown without the drawer -- an embedded meeting or Virtual
Participant page, or the meeting list's preview of a selected meeting -- shows
the same feed above the meeting's tabs, in a section that can be folded.

**The count in the top navigation.** The bell in the top navigation counts the
notifications that arrived **during the current visit to LMA**, for meetings
you own or that are shared with you, other than the meeting you have open. Its
menu lists those meetings and links to each. The count is not stored: a
notification published while you were not using LMA is not counted (it is in
that meeting's own feed), and the count starts again when the page is reloaded.
Opening a meeting clears its count. An Admin receives every meeting's
notifications, as for the meeting list, but the count includes only the
meetings the Admin owns or that are shared with them.

## Who sees them

The people who receive a notification are decided from the meeting, never by
whoever publishes it, and nothing in the stored notification records them.

- **Live delivery.** The publish resolver reads the meeting and puts its
  current `Owner` and `SharedWith` on the published message. The
  `onMeetingNotification` subscription is filtered on those two fields by the
  same filter as the meeting's other live updates (`subscription.js`), so a
  message reaches the owner, the users the meeting was shared with at that
  moment, and Admins.
- **Reading.** `listMeetingNotifications` first applies the meeting page's own
  access rule (`meetingAccess.js`, the rule `getCall` applies) to the meeting
  as it is at the time of the read.

So a user the meeting is shared with later sees its earlier notifications as
soon as they open it, and receives later ones live. A user whose share is
removed can no longer read them, and receives no notification published after
the removal. A page that user already had open keeps what it had loaded until
it is reloaded, as it does for the transcript.

## Dismissing and muting

Dismissing and muting change only your own view, and are stored with your
account, so they apply in every browser you sign in from.

- **Dismiss** (the × on a notification) hides that notification for you.
  **Show dismissed** lists dismissed notifications again, each with
  **Restore**. A notification that a feature later updates stays dismissed.
- **Mute** › **Mute *category* on this meeting** hides that category on this
  meeting.
- **Mute** › **Mute *category* on every meeting** hides it on every meeting
  and leaves it out of the count in the top navigation. **Notification
  settings**, in the bell's menu, lists the categories you muted on every
  meeting and shows each one again.

The other people who can see the meeting are not affected. The notifications
stay stored, and MCP tools return them whatever you dismissed or muted.

## Buttons on a notification

A notification can carry up to two buttons, each `{Label, Kind, Payload}`:

| Kind | Payload | Pressing it |
|---|---|---|
| `COPY` | `{"text": "..."}`, up to 2000 characters | Copies the text to the clipboard. Nothing is sent to LMA. |
| `MUTATION` | `{"action": "<name>", "args": {...}}` | Runs that action's own GraphQL operation, as the signed-in user. |

A `MUTATION` button never carries an operation. Its payload names an action
from a fixed list, which the publish resolver
(`notificationPublishMeeting.js`) and the page
(`notification-actions.js`) both hold. The publish is refused if the action is
not on the resolver's list, or if it has an argument the action does not take.
The page shows no button for an action it does not know. The operation runs
with the user's own sign-in, so its resolver decides whether that user may do
it. The meeting it acts on is always the notification's own meeting, never one
named in the payload.

The list is empty in this release, so only `COPY` buttons can be published. An
action is added by one entry on each side: its name and the argument names it
takes in the resolver, and its operation and variables in the page. A contract
test holds the two lists to each other.

## Retention

- **Guidance** (category `guidance`) may quote the meeting, so it follows the
  **transcript**. It expires with the transcript (**Days to keep transcript
  segments**), and the [transcript deletion](transcript-deletion.md) function
  deletes it when the transcript is deleted after the summary. That includes
  the guidance an MCP client pushed with `push_meeting_guidance`.
- **Other categories**, and each user's dismissals and mutes on a meeting,
  follow the **meeting record** (**Days to keep meeting records**).
- **Deleting a meeting** deletes all of its notifications, the users'
  dismissals and mutes on it, and its rate counters.
- **A user's categories muted on every meeting** are deleted when the user is
  deleted from User Management.

No notification is kept longer than its meeting.

## Reading them over MCP

The hosted MCP server's `list_meeting_notifications` tool, which a **Read only**
key may call, returns a meeting's notifications to the people who can see the
meeting:

```
list_meeting_notifications(meeting_id, category?, since?, limit?, cursor?)
```

- `category` keeps one category, such as `guidance`.
- `since` (ISO 8601) keeps those created **or updated** at or after that time,
  so a client that polls with the latest `updatedAt` it has seen also receives
  updates.
- `limit` defaults to 100 and is at most 500. Pass the answer's `nextCursor` as
  `cursor` for the next page. `category` and `since` are applied to the rows as
  they are read, so the tool reads up to ten pages to fill `limit`; an answer
  can still be short, or empty, with a `nextCursor`. Keep following
  `nextCursor` until it is absent.

Each item has `notificationId`, `category`, `type`, `severity`, `title`, `body`,
`refs`, `dedupKey`, `actions`, `source`, `createdBy`, `createdAt`, `updatedAt`,
`revision` and `expiresAt`, oldest first. `list_meeting_guidance` returns the
`guidance` category in its own, older shape (see
[Meeting Guidance](mcp-api-key-auth.md#meeting-guidance)).

The tool wraps the `listMeetingNotifications` GraphQL query, which the meeting
page uses too:

```graphql
listMeetingNotifications(CallId: ID!, Category: String, Since: AWSDateTime,
  Limit: Int, NextToken: String, NewestFirst: Boolean): MeetingNotificationPage
```

## Publishing a notification from a backend function

A backend Lambda function publishes with its own IAM role, and needs no user
token. It uses the helper in the configuration layer, which every Python
function in the AI stack carries:

```python
from lma_notifications import publish_meeting_notification

publish_meeting_notification(
    call_id,
    category="guidance",
    type="ask_question",
    title="Ask who signs off on the budget",
    body="Nobody has named an owner yet.",
    severity="suggestion",
    refs=["checklist:item-3", "segment:seg-42"],
    dedup_key="ask_question:item-3",
    source="my-feature",
)
```

The function needs `appsync:GraphQL` on
`.../types/Mutation/fields/publishMeetingNotification` and the
`APPSYNC_GRAPHQL_URL` environment variable. The helper sends the IAM-only
`publishMeetingNotification` mutation. A function that publishes on behalf of a
user, as the MCP server does, checks first that the user may see the meeting.

| Argument | Meaning |
|---|---|
| `call_id` | The meeting's CallId. A meeting that does not exist is refused (`NotificationMeetingNotFound`). |
| `category` | Lower-case letters, digits, `_` and `-`, starting with a letter, at most 32 characters. |
| `type` | A name within the category: letters, digits and `_ . : / -`, at most 64 characters. |
| `title` | At most 200 characters, or `None` for the category's own heading. |
| `body` | Markdown, at most 2000 characters. |
| `severity` | `info` (the default), `suggestion`, `warning` or `urgent`. |
| `refs` | Up to 20 opaque strings of up to 200 characters, stored and returned exactly as given. |
| `dedup_key` | See [Deduplication](#deduplication). Same characters as `type`, at most 200. |
| `expires_at` | When the page stops showing it: a `datetime` or an ISO 8601 string. Optional. |
| `actions` | Up to two `{"label", "kind": "copy" or "mutation", "payload"}`; see [Buttons](#buttons-on-a-notification). |
| `source` | Who published it, such as `mcp` or the feature's name. |
| `created_by` | The user it is published on behalf of, if any. |

It returns the stored notification. `Created` is false when the publish updated
an earlier one. A notification of category `guidance` is given the transcript's
expiry. Every notification's expiry is capped at the meeting's.

### Deduplication

A second publish with the same meeting, **category** and `dedup_key` updates
the notification the first one made, instead of adding another. A feature that
re-evaluates a meeting every minute or two can therefore publish the same item
each time. The update keeps the notification's id and `CreatedAt`, so it stays
where it was in the feed, adds one to its `Revision`, sets `UpdatedAt`, and
replaces every other field with the new publish's values. It is published again,
so open pages show the change. A publish without a `dedup_key` always adds a
notification.

### Rate limit

A meeting accepts **120 publishes in each UTC minute**, inserts and updates
together. Beyond that, the helper raises `NotificationRateLimited`. The limit is
there to stop a publisher that runs away; a feature should keep its own, much
lower, limit.

## How it is stored

Everything is in the AI stack's event-sourcing table, under one partition per
meeting, `PK = nt#<CallId>`:

| Row | Sort key | Holds |
|---|---|---|
| A notification | `n#<CreatedAt>#<NotificationId>` | Its fields, and its expiry (`ExpiresAfter`) |
| A dedup pointer | `d#<Category>#<DedupKey>` | The notification the first publish with that key made |
| A rate counter | `r#<minute>` | The publishes in that minute; expires after an hour |
| A user's view | `u#<username>` | `Dismissed` (at most 1000 ids) and `MutedCategories` |

A user's categories muted on every meeting are one row, `PK = usr#<username>`,
`SK = notification-prefs` (at most 50 categories).

The publish is an AppSync pipeline of JavaScript resolvers, with no Lambda
function: `notificationPublishMeeting.js` checks the input and reads the
meeting; `notificationPublishRate.js` counts the publish;
`notificationPublishDedup.js` finds or creates the dedup pointer in one
conditional write; `notificationPublishWrite.js` stores the notification and
answers with it, with the meeting's `Owner` and `SharedWith`, which AppSync
publishes to `onMeetingNotification`.

The signed-in user's view is read and changed with
`getMyMeetingNotificationState` / `updateMyMeetingNotificationState` (after the
meeting's access check) and `getMyNotificationPreferences` /
`updateMyNotificationPreferences`. Each is keyed by the caller's own identity,
never by an argument.

## Cost

A publish costs one AppSync request and a few DynamoDB reads and writes (the
meeting, the counter, the pointer, the notification), plus one AppSync message
to each open page that receives it. Nothing in the path calls a Lambda function
or a model. Reading over MCP runs inside the hosted MCP server's existing
function.

## Limitations

- The count in the top navigation covers the current visit only, as described
  [above](#on-the-meeting-page).
- The meeting page loads at most the 1000 most recent of a meeting's
  notifications; older ones are still returned over MCP.
- After a share is removed, a page that user already had open keeps what it had
  loaded until it is reloaded.
- No `MUTATION` button action is offered yet; only `COPY` buttons can be
  published.
- Notifications are delivered in LMA only. There is no email, chat or other
  channel, and adding one would be a separate, per-category choice.

## Related

- [MCP API Key Authentication](mcp-api-key-auth.md): `push_meeting_guidance`,
  `list_meeting_guidance` and `list_meeting_notifications`
- [User-Based Access Control](user-based-access-control.md)
- [Transcript Deletion After the Summary](transcript-deletion.md)
- [Privacy and Data Handling](privacy-and-data-handling.md)
