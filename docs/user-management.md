---
title: "User Management"
---

# User Management

LMA ships with a built-in **User Management** page that lets administrators create and delete users, and choose each user's role, directly from the Web UI, without having to log into the AWS console. The feature is **admin-only** and is enforced at three layers for defense in depth.

## Table of Contents

- [Overview](#overview)
- [Roles](#roles)
- [Accessing the User Management page](#accessing-the-user-management-page)
- [Creating a user](#creating-a-user)
  - [Inviting a new user to a meeting](#inviting-a-new-user-to-a-meeting)
  - [What the invitation says about consent](#what-the-invitation-says-about-consent)
- [Changing a user's role](#changing-a-users-role)
- [Deleting users](#deleting-users)
- [Last sign-in](#last-sign-in)
- [Last active](#last-active)
- [Exporting users for an access review](#exporting-users-for-an-access-review)
- [Security model](#security-model)
- [Guard rails](#guard-rails)
- [Email domain restrictions](#email-domain-restrictions)
- [Participant accounts for a meeting](#participant-accounts-for-a-meeting)
- [Troubleshooting](#troubleshooting)

---

## Overview

Users are stored in LMA's Amazon Cognito User Pool (created by the `lma-cognito-stack` nested stack). The first admin user is created automatically at deployment time from the `AdminEmail` CloudFormation parameter.

The User Management page provides:

- A searchable table of every user in the user pool, showing email, role, Cognito status (e.g. `CONFIRMED`, `FORCE_CHANGE_PASSWORD`), enabled flag, creation date, [last sign-in](#last-sign-in) and [last active](#last-active) time.
- **Create user** — creates a Cognito user with the chosen role. Cognito emails the new user a temporary password; they must set a new password on first sign-in.
- **Change role** — moves one selected user to another role.
- **Delete user(s)** — permanently removes one or more selected users from Cognito, with confirmation. The consents a deleted user gave before meetings are withdrawn, so no later meeting counts them; their records stay until they expire (see [Consent given in advance](meeting-consent.md#withdrawing)).

## Roles

LMA uses three roles:

| Role            | Cognito group | Permissions                                                                                                  |
|-----------------|---------------|--------------------------------------------------------------------------------------------------------------|
| **Admin**       | `Admin`       | Full access: all meeting features **and** admin-only pages (User Management, MCP Servers, View / Edit Configuration). |
| **User**        | *(none)*      | Standard access: Meetings List, Meetings Query, Stream Audio/Video, Virtual Participant, MCP API Keys. No admin pages. |
| **Participant** | `Participant` | Reads the meetings shared with them, read-only, asks the meeting assistant about them, and enrolls their own voice. No other meetings, no meeting sources, MCP access or configuration. See [Participant](user-based-access-control.md#participant). |

> The `Admin` and `Participant` Cognito groups are created by `lma-cognito-stack`. Membership determines the role: `Admin` first, then `Participant`; a user that is in neither group is a regular `User`. Every token the user pool issues names that role, so the role applies everywhere a token is accepted. [User-Based Access Control](user-based-access-control.md#roles) lists what each role can use.

## Accessing the User Management page

Sign in to the LMA Web UI as an Admin user. In the left-hand side navigation, expand the **Configuration** section and choose **User Management**. The URL is:

```
https://<your-cloudfront-domain>/#/configuration/user-management
```

Non-admin users do not see this link in the navigation, and if they navigate directly to the URL, they get an **Access denied** message. The GraphQL API also refuses their requests.

## Creating a user

1. On the **User Management** page, choose **Create user**.
2. Enter the user's **email address** (used as the Cognito username).
3. Select a **role** — `User`, `Admin` or `Participant`.
4. Optionally, choose a **Meeting to invite them to** (see [Inviting a new user to a meeting](#inviting-a-new-user-to-a-meeting)).
5. Choose **Create**.

The new user receives a **Welcome to Live Meeting Assistant!** email from Amazon Cognito that includes their temporary password and a clickable link to the LMA Web UI. On first sign-in they are prompted to set a new password. What else the email says depends on the role, on whether consent given in advance is allowed, and on whether a meeting was chosen; see [What the invitation says about consent](#what-the-invitation-says-about-consent).

### Inviting a new user to a meeting

The **Meeting to invite them to** field is optional, for any role. It offers the Virtual Participants that are scheduled or running and the meetings of the last 14 days, and also takes a pasted meeting identifier (`CallId`), Virtual Participant id, or a link to a meeting page, a Virtual Participant page or a meeting's consent page. Without a meeting, the account and its email are exactly as described above.

With a meeting:

- the invitation email also names the meeting and gives the link to its consent page, as the invitations of [Participant accounts for a meeting](#participant-accounts-for-a-meeting) do;
- the address is added to the end of the meeting's [participant list](virtual-participant.md#participant-list), with no name, if it is not on it already. The list changed is the meeting's once the meeting record exists, and the Virtual Participant's own list until then, as on the Virtual Participant's page. With **Meeting Sharing › Share meetings with their listed participants** on (the default), the meeting is then shared with the address.

The meeting is checked before anything is changed: a meeting or Virtual Participant id that does not exist, a participant list that already holds 100 people, or an address that already has an account is refused, and no account is created. The address is added to the list before the account is created, so if Cognito then refuses the account (for example because its daily email limit is reached), the address stays on the list.

### What the invitation says about consent

When **Meeting Consent › Allow consent given in advance** is on (the default), the invitation also says what the **My consents** page is for, with a link to it (`<web UI address>/#/consents`):

- a **Participant**'s invitation has a short paragraph: meetings recorded with LMA ask everyone for consent; to avoid being asked in every meeting, the person can record their choice after signing in, from a meeting's consent link, for that meeting or for the meetings its organizer holds, for a period they choose; their choices are on the My consents page, where they can change or withdraw them at any time; and they can still object in any meeting. The paragraph does not ask for consent;
- a **User**'s or **Admin**'s invitation has one line: *"You can manage your meeting consents on the My consents page."*

The setting is read when the invitation is sent, from the active configuration, the same way the consent page reads it. When it is off, or cannot be read, the invitation says nothing about consent given in advance. The same applies to the invitations of [Participant accounts for a meeting](#participant-accounts-for-a-meeting), which are always Participant invitations. The first Admin's invitation, sent when the stack is created, and accounts created outside LMA (for example in the Amazon Cognito console) have no such paragraph. An invitation for a meeting keeps the meeting's name and consent link in either case.

> The Web UI URL in the email is generated by a Cognito `CustomMessage` Lambda trigger that reads the CloudFront endpoint from the LMA Settings SSM parameter (populated by `AISTACK` on every stack update). The very first admin user — created at initial stack-create time before `AISTACK` has populated the parameter — receives a fallback version of the email that refers the user to the CloudFormation stack's `ApplicationCloudfrontEndpoint` Output.

## Changing a user's role

1. Select one user in the users table.
2. Choose **Change role**.
3. Select the new role and choose **Change role**.

The user's Cognito group membership changes at once. Their role changes at their next sign-in or token refresh, which is at most an hour later; until then they keep the role they had when they signed in. The hosted MCP server's API key endpoint reads the role on every request, so it applies there immediately: a key belonging to a user who is now a Participant is refused, and works again if they become a User or an Admin.

You cannot change your own role, and the last remaining Admin cannot be moved to another role.

## Deleting users

1. Select one or more rows in the users table.
2. Choose **Delete**.
3. Review the confirmation modal and choose **Delete** to proceed.

Deletion removes the user from the Cognito user pool, together with their recorded [last sign-in](#last-sign-in) and [last active](#last-active) times. (A user deleted directly in Cognito, for example in the console, leaves that item in the sign-in table.) It does **not** delete meetings that user owns or has shared.

## Last sign-in

The **Last sign-in** column shows when each user last signed in to LMA with their password. It is meant for periodic user access reviews: finding accounts nobody has used for a while, so they can be disabled or deleted.

**How it is recorded.** Amazon Cognito's `ListUsers` API does not return a sign-in time, and reading a user's sign-in history from Cognito requires Cognito threat protection, which LMA does not require. So the `lma-cognito-stack` nested stack adds a Cognito **PostAuthentication** Lambda trigger, `CognitoPostAuthenticationFunction`. After each successful sign-in it writes the time to a small DynamoDB table, `UserSignInTable`, one item per user keyed by the user's Cognito `sub` (an identifier that never changes). The table is encrypted with the stack's customer-managed KMS key and, like the user pool, is kept when the stack is deleted with `EnableDataRetentionOnDelete=true`. No attribute is added to the user pool. A failed write does not fail the sign-in: the trigger logs the failure in `/<stack-name>/lambda/CognitoPostAuthenticationFunction` and returns, and the user signs in as usual. As with any Cognito trigger, if Lambda cannot run the function at all (for example, it is throttled or does not answer within Cognito's five-second limit), Cognito fails that sign-in and the user can try again.

**What counts as a sign-in.** PostAuthentication runs when a user authenticates with their password: in the web UI, in the browser extension, or through the MCP external app client. It does **not** run when a client silently renews its session with a refresh token, which the web UI does while a user stays signed in (for up to 30 days, the app client's refresh-token lifetime). So the column is the last *interactive sign-in*, not the last time the user used LMA: a user who keeps a browser session open can be active today and still show a sign-in from several weeks ago. For an access review, read an old or missing time as "has not entered their password since then", and use [Last active](#last-active) for when the account was last used.

**What the column can show:**

| Value | Meaning |
|---|---|
| A date and time | The user's last recorded sign-in, in your browser's time zone. |
| **Not recorded** | No sign-in has been recorded for this user. Sign-in times are recorded from the first deployment of a version of LMA that includes this feature onwards, so right after an upgrade every user shows this until they next sign in. A new user who has not yet signed in for the first time also shows it. |
| **Unavailable** | The sign-in times could not be read, so nothing is known about any user's last sign-in. The resolver's log (`/<stack-name>/lambda/UserManagement`) says why. |

## Last active

The **Last active** column shows when each user last used LMA, as far as LMA can tell without recording every request. For an access review it is usually the column to sort on: an account whose last activity is months old is a candidate for disabling, whatever its last sign-in.

**What counts as activity.** Three things, each recorded in the same `UserSignInTable` item as the last sign-in, in a `LastActiveAt` attribute:

- **An interactive sign-in**, in the web UI, the browser extension or through the MCP external app client.
- **A token renewal.** Every client that stays signed in (the web UI, the browser extension, the desktop app) renews its tokens with its refresh token about once an hour while it is open, and each renewal counts. Cognito's pre token generation trigger, `CognitoPreTokenGenerationFunction`, which also sets each token's [role](#roles), runs on every sign-in and every renewal and writes the time.
- **A request with the user's MCP API key.** API keys never reach Cognito, so the hosted MCP server's API key authorizer, `MCPApiKeyAuthorizerFunction`, writes the time when it admits a request, together with the key's own last use (shown in the MCP API Key container on the MCP Servers page; see [MCP API key authentication](mcp-api-key-auth.md#last-used)).

**Resolution.** Each writer updates the time at most once every 15 minutes (a conditional DynamoDB update that does nothing when the recorded time is less than 15 minutes old), so the time can be up to 15 minutes earlier than the user's latest activity. For a signed-in client the activity itself is only seen when a token is renewed, so a user who has the web UI open is shown as active to within about an hour, and a user who closed it shows roughly when it was last renewed. An MCP API key in use is seen to within 15 minutes.

**What is not counted.** Individual page loads, GraphQL calls and meeting streams between two renewals are not recorded separately, and nothing is recorded for a client that is closed, even if its refresh token is still valid. Calls made with AWS credentials rather than with an LMA user's tokens (as `lma users list` and the other CLI and SDK operations that act with your AWS credentials are) are not LMA user activity and are not recorded; a CLI or SDK operation that signs in as an LMA user counts like any other sign-in. A Virtual Participant or a meeting stream that runs on after the user's client is closed does not count either.

**Failures never block a user.** Each write is one attempt that gives up after at most 1.5 seconds, and a failure is logged (in `/<stack-name>/lambda/CognitoPreTokenGenerationFunction` or the authorizer's log group) without affecting the token or the request.

The column shows **Not recorded** for a user with no recorded activity (activity is recorded from the first deployment of a version of LMA that includes it onwards; a user who signs in or whose open client renews its tokens shows a time from then on) and **Unavailable** when the times could not be read, as for Last sign-in.

## Exporting users for an access review

The [LMA CLI](lma-cli.md#lma-users-list--list-users-for-an-access-review) lists the same information from a terminal, for an access review export:

```bash
# A table in the terminal
lma users list --stack-name LMA

# A CSV file for the review: email, username, role, status, enabled, created_at, last_sign_in_at, last_active_at
lma users list --stack-name LMA --format csv --output lma-users-2026-11.csv

# JSON, with the stack, Region, account, user pool and time of the export
lma users list --stack-name LMA --format json
```

A CSV row looks like this (times are ISO 8601 in UTC):

```
email,username,role,status,enabled,created_at,last_sign_in_at,last_active_at
jdoe@example.com,jdoe@example.com,User,CONFIRMED,true,2026-03-02T14:05:11.000Z,2026-11-20T09:30:00.000Z,2026-11-24T16:45:00.000Z
new.user@example.com,new.user@example.com,User,FORCE_CHANGE_PASSWORD,true,2026-11-23T12:00:00.000Z,Not recorded,Not recorded
```

In the CSV and table, a user with no recorded sign-in (or activity) shows `Not recorded`, and every user shows `Unavailable` when the times could not be read (with a warning on standard error saying why, for example a stack not yet updated to a version that records them). In JSON, `last_sign_in_at` and `last_active_at` are `null` in both cases, and `last_sign_in_available` (`false` when the times could not be read) tells them apart.

The command runs with your AWS credentials, not as an LMA user, so it needs IAM permissions on the stack's resources rather than membership of the `Admin` group. The permissions are listed under [`lma users list`](lma-cli.md#lma-users-list--list-users-for-an-access-review).

## Security model

Authorization for user management is enforced at **three independent layers**:

1. **AppSync schema** — `listUsers`, `createUser`, `setUserRole` and `deleteUser` are declared with `@aws_cognito_user_pools(cognito_groups: ["Admin"])`. Non-admin callers are rejected by AppSync before the Lambda resolver is even invoked. (Note: `@aws_cognito_user_pools` is the correct directive when the API uses more than one auth type — `@aws_auth` only works for single-auth APIs.)
2. **Lambda resolver** — `UserManagementFunction` re-checks the caller's `cognito:groups` claim from the signed JWT that AppSync passes through. This is belt-and-braces protection in case a resolver is ever wired to a field without the directive.
3. **Web UI** — the User Management navigation link and page are hidden from non-admin users. Direct navigation shows an access-denied message.

The same Lambda function also answers `getMeetingParticipantAccounts` and `createParticipantAccounts`, which name `Admin` and `User` in their directives and apply the meeting-owner checks described in [Participant accounts for a meeting](#participant-accounts-for-a-meeting). Each operation's roles are listed in the function itself and checked against the [role matrix](user-based-access-control.md#roles) by its unit tests.

The Lambda's IAM role grants only the specific `cognito-idp` actions required to manage users, and is scoped to the LMA user pool ARN:

```
cognito-idp:ListUsers
cognito-idp:ListUsersInGroup
cognito-idp:AdminGetUser
cognito-idp:AdminCreateUser
cognito-idp:AdminDeleteUser
cognito-idp:AdminAddUserToGroup
cognito-idp:AdminRemoveUserFromGroup
cognito-idp:AdminListGroupsForUser
```

It may also read (`dynamodb:BatchGetItem`) and delete (`dynamodb:DeleteItem`) items in the Cognito stack's `UserSignInTable`, for the [last sign-in](#last-sign-in) and [last active](#last-active) times it lists and the item of a user it deletes. The PostAuthentication and pre token generation triggers' roles, and the MCP API key authorizer's, may only update items in that table (the authorizer also reads a key owner's `sub` with `cognito-idp:AdminGetUser` when the key does not store it).

## Guard rails

To reduce the risk of accidental admin lock-out:

- **No self-delete or self-demotion.** An admin cannot delete their own account or change their own role.
- **Last-admin protection.** The last remaining Admin user can be neither deleted nor moved to another role. Make another user an Admin first.
- **Email validation.** The email address must be well-formed.

If a rule is violated, the API returns a clear error and the UI shows it in the dialog.

## Email domain restrictions

If you set the `AllowedSignUpEmailDomain` CloudFormation parameter at deployment time (for example, `example.com` or a comma-separated list such as `example.com,partner.com`), then new users created via **User Management** must have an email address in one of those domains. The restriction is enforced server-side in the Lambda in addition to Cognito's sign-up trigger. Leave the parameter empty to allow any email domain.

## Participant accounts for a meeting

Besides this page, there is one other way an account is created: from a meeting's [participant list](virtual-participant.md#participant-list). The meeting's owner, or an Admin, can choose **Create accounts for participants without one** on the meeting page or the Virtual Participant's page. This is its own operation (`createParticipantAccounts`), separate from **Create user**, with its own rules:

- **Admins and Users may call it, for one meeting at a time.** A User may do so only for a meeting they own, not for one shared with them. An Admin may do so for any meeting. A Participant may not.
- **Only people on that meeting's list.** Every address in the request must be on the meeting's participant list (compared ignoring case); a request naming anyone else is refused as a whole.
- **Only the Participant role.** Each new account is added to the `Participant` group and nothing else. An address that already has an account is reported as existing and its account is not changed. Accounts with the User or Admin role, and changes of role, are made only on this page, by an Admin.
- **Email domain restrictions apply.** When `AllowedSignUpEmailDomain` is set, an address outside those domains is reported as not allowed and no account is created for it, exactly as for **Create user** (see [Email domain restrictions](#email-domain-restrictions)).
- **Limits.** One request creates at most 25 accounts; choose the button again for the rest. A User may create at most 25 accounts this way per day (UTC), counted across all their meetings; the answer says how many remain. A User may also look up the accounts of at most 1,000 different addresses a day (UTC); an address counts once a day however often its list is read, and the day's count keeps only a hash of each address. This is so that a list cannot be rewritten over and over to find out who has an account. Admins have neither daily limit.

Cognito sends each new account the same invitation email as **Create user**, with one paragraph more: it names the meeting and gives the link to its consent page, where the person can say, once signed in, whether they agree to the meeting's being recorded and transcribed (see [Consent given in advance](meeting-consent.md#consent-given-in-advance)), and the Participant paragraph about the My consents page described in [What the invitation says about consent](#what-the-invitation-says-about-consent). The user pool's CustomMessage trigger builds these paragraphs, for the admin-created invitations only, from the meeting's name and identifier, the account's role and the consent setting sent with the request, and the web UI's own address; every other Cognito message is unchanged. On a Virtual Participant's page the list read and acted on is the meeting's once the Virtual Participant has created its meeting, and its own until then. With the user pool's default email configuration (`COGNITO_DEFAULT`), Cognito sends only a small number of emails per day for the whole user pool (50 at the time of writing; see [Email settings for Amazon Cognito user pools](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-email.html)). Every invitation, password reset and verification email counts against it, so creating accounts for a large meeting can use it up. A deployment that needs to invite more people should configure the user pool to send email through Amazon SES.

A Participant account created this way reads the meetings shared with it. With
**Meeting Sharing › Share meetings with their listed participants** on (the
default), a meeting is shared with the people on its participant list as soon
as it starts and whenever the list changes, so a listed person who signs in
finds it under **Shared meetings**; see
[Sharing with the participant list](user-based-access-control.md#sharing-with-the-participant-list).

To answer the meeting page's account status (`getMeetingParticipantAccounts`), the same Lambda function reads the meeting's owner and participant list (`dynamodb:GetItem` on the meeting records and the Virtual Participant table) and looks up each address in the user pool. For **Create user** with a meeting it also appends the address to that list (`dynamodb:UpdateItem` on the meeting records and the Virtual Participant table), and for every invitation it reads the Meeting Consent section of the configuration (`dynamodb:GetItem` on the configuration table). The two daily counts are items in the meeting table under the user's `usr#` key, which expire after two days.

## Troubleshooting

**"Unauthorized" or "Not Authorized" error when opening the page**
: You are not a member of the `Admin` Cognito group. Ask an existing Admin to change your role to `Admin` on this page, then sign out and sign in again.

**Cognito invitation email never arrives**
: Check the recipient's spam folder first. Cognito sends the email from `no-reply@verificationemail.com` by default. If the email never arrives, an existing Admin can reset the temporary password in the Cognito console (*Users → [user] → Reset password*).

**"Cannot delete the last remaining Admin user"** or **"Cannot change the role of the last remaining Admin user"**
: Make another user an Admin first. This guard prevents the deployment from becoming unmanageable.

**A user's new role has not taken effect**
: A role change applies at the user's next sign-in or token refresh, at most an hour later. Ask them to sign out and sign in again to apply it at once.

**"Create accounts for participants without one" reports "limit reached"**
: Either one request reached its 25 accounts (choose the button again) or you have created 25 participant accounts today (an Admin can create the rest, or try again the next day, UTC).

**"You have looked up the accounts of 1000 different addresses today"**
: The account status, and the button, look up each address on a meeting's list. Each different address counts once a day (UTC), however often the list is read again; after 1,000 different addresses in a day, a User's further lookups of new addresses are refused until the next day. An Admin is not limited.

**"Email domain 'X' is not allowed"**
: The `AllowedSignUpEmailDomain` CloudFormation parameter restricts new user emails to specific domains. Update the parameter on your stack and redeploy, or choose an email in an allowed domain.

**Every user shows "Not recorded" in the Last sign-in column**
: Sign-in times are recorded from the deployment of this feature onwards. Users show a time after their next sign-in with their password; a user who stays signed in (their session renewed by a refresh token) does not, until they sign in again. See [Last sign-in](#last-sign-in).

**The Last sign-in column shows "Unavailable"**
: The user management Lambda could not read `UserSignInTable`. Check `/<stack-name>/lambda/UserManagement` for the error. On a stack whose AI stack was deployed without the Cognito stack's table name (for example, a nested-stack update that did not complete), update the whole LMA stack again.

**CloudWatch Logs for debugging**
: The Lambda logs all admin actions (create, role change, delete) and all attempts by non-admin callers. Look in CloudWatch Logs under `/<stack-name>/lambda/UserManagement`.
