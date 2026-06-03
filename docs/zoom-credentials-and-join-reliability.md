---
title: Zoom Sign-in & Join Reliability
sidebar_label: Zoom Sign-in
---

# Zoom Sign-in & Join Reliability

LMA's Virtual Participant (VP) joins Zoom meetings via headless Chromium. A guest join from a fresh, automation-driven browser is the least reliable path — Zoom may require an account, or present a verification step the VP can't complete on its own. This page describes the features LMA ships to make Zoom joins reliable, and how to use them.

## Browser stack

- The VP drives **[CloakBrowser](https://github.com/CloakHQ/cloakbrowser)** (a patched Chromium build, pinned at 0.3.31) via `playwright-core`. Its newer Chromium handles Zoom's web-client video encoder reliably even on a small 2-vCPU host (`t3.medium`) — the older stock Debian Chromium threw Zoom's "Something went wrong" error and turned the VP camera off under the same load. Enabling the **Simli avatar** makes the browser's WebRTC behaviour more conspicuous, which makes Zoom more likely to require a login or present a CAPTCHA — see [Simli and join reliability](#simli-and-join-reliability).
- A per-user **persistent profile** (cookies, "trusted device" markers) is restored from S3 on launch and saved back at meeting end, so a user who has signed in once is recognised on subsequent meetings (see [Persistent Chromium profile per user](#persistent-chromium-profile-per-user) below).
- Fresh profiles run a short **warmup** (Google → HN → Wikipedia → the meeting-platform home pages) before navigating to the meeting URL, so a first-time profile arrives with normal browsing history rather than zero state.

## Simli and join reliability

Enabling the **Simli avatar** (both `SimliApiKey` and `SimliFaceId` set) requires browser settings that bridge the avatar's video into the meeting. Those settings change the browser's WebRTC behaviour in a way that meeting platforms are more likely to flag, so **with Simli enabled a Zoom join is more likely to require a login or trigger a CAPTCHA** before it lets the participant in.

> **If you are hitting login prompts or CAPTCHA challenges on join and don't need the on-camera avatar, disable Simli** by leaving `SimliApiKey` and `SimliFaceId` unset. Without Simli the VP joins with the most reliable profile. If you do want the avatar, signing in with [per-user Zoom credentials](#per-user-zoom-credentials) makes joins far more reliable.

## Per-user Zoom credentials

Each LMA user can store **one** set of Zoom credentials. When a user starts a virtual participant for a Zoom meeting and ticks **"Sign in with my stored Zoom account when joining this meeting"**, the VP signs in to Zoom before navigating to the meeting URL. A signed-in session joins far more reliably and allows the VP to join meetings that disallow guests.

### How it works

- The credentials live in **AWS Secrets Manager** under `${StackName}/zoom-credentials/{cognitoSub}`, encrypted with your stack's customer-managed KMS key.
- The Lambda that writes the secret runs as the user; the Cognito sub is taken from the AppSync identity, so a user can only set their own credentials.
- The plaintext password is **never returned** to the React UI. The status query returns only `{ present, username, lastUpdatedAt }`.
- The VP container reads the secret at runtime; it is not put on the task definition or in the Step Functions execution input.
- Removing the credentials triggers a 7-day Secrets Manager scheduled-deletion **and** wipes the user's persisted Chromium profile prefix in S3 (so cached cookies don't outlive the credentials).

### How to enable

1. Open **Virtual Participants** in the LMA UI.
2. Click **Create Virtual Participant**, choose **Zoom** as the platform.
3. In the **Zoom account (Optional)** section, click **Add Zoom credentials** (use the link to create a new Zoom account if you don't have one).
4. Save the credentials. Tick **Sign in with my stored Zoom account when joining this meeting** before clicking **Join Now**.

### Caveats

- **CAPTCHA / 2FA still happens.** When Zoom presents a verification challenge during sign-in, the VP escalates to `MANUAL_ACTION_REQUIRED` and the React UI surfaces a Flashbar alert + the live noVNC viewer. Solve the challenge there; the VP picks up automatically when the session is authenticated.
- **Brand-new accounts join less reliably.** Sign in to Zoom on your own laptop with the account at least once *before* relying on LMA — an account whose only activity is joining meetings from AWS IP ranges is more likely to hit a verification step.

## AI-driven sign-in loop

Zoom's sign-in flow is not a single page — after the password is accepted, the user typically sees a sequence of post-login interstitials (passkey-binding upsell, phone-binding upsell, "verify your email" notices, OTP entry, regional-disclosure consent, etc.) before landing on the dashboard. The set and order of these pages changes over time and per-account.

The VP delegates each post-username step to Claude (Bedrock, vision-capable). After submitting the username, the sign-in driver enters a loop:

1. Take a screenshot + a compact DOM summary of the current page.
2. Ask Claude what to do next. Claude returns one of: **fill_password** (selector + reason), **skip** (selector + reason), **continue** (the page is loading, just wait), **wait**, **needs_human** (escalate to MANUAL_ACTION_REQUIRED), or **done** (we're authenticated).
3. Execute the action; loop.

This keeps the deterministic logic out of the codebase and lets the VP tolerate Zoom's frequent sign-in flow changes without a code/deploy cycle.

The same AI navigator handles the post-login interstitial sequence on the way to the meeting URL, and the in-meeting unknown-dialog watchdog (consent, recording-notice, verification prompts, etc.).

## Persistent Chromium profile per user

The VP container hydrates a per-user `userDataDir` from S3 at launch and uploads it back at meeting end. Once the user has signed in once and cleared any verification challenge via the VNC viewer, Zoom plants a "trusted device" cookie that persists. Subsequent meetings reuse that cookie, so the VP signs in cleanly without re-prompting.

- Profile path: `s3://${StackName}-vp-profiles-${AccountId}/profiles/{cognitoSub}/{platform}/`.
- Concurrency lock: `lock.json` at the same prefix, with a 10-minute expiry. If two meetings start for the same user simultaneously, the second falls back to a fresh profile.
- Lifecycle: profiles are kept for 365 days of inactivity, then expired. Removing a user's Zoom credentials deletes their profile prefix immediately.
- Bucket has KMS-CMK encryption at rest, all-public-access blocked, versioning enabled.

## AI-driven DOM resolver fallback

Independently of the sign-in feature, every platform handler wraps its hardcoded CSS selectors in a fallback resolver that asks Claude (Bedrock) to find the right element when the primary selectors miss. This makes the VP self-heal when Zoom/Teams/Webex/Chime ship UI changes.

- Model: `us.anthropic.claude-haiku-4-5-20251001-v1:0` (override via `BEDROCK_DOM_RESOLVER_MODEL_ID`; set to empty string to disable the fallback entirely).
- Successful resolutions are cached in the **DomSelectorCache** DynamoDB table (shared across all VP tasks, 30-day TTL on `lastUsedAt`). The first meeting after a Zoom UI change pays the Bedrock-call cost; every subsequent meeting hits the cache.
- Unknown popup dialogs that the existing keyword auto-dismiss handler can't classify are sent to Claude as `analyzeUnknownDialog`. CONSENT/RECORDING_NOTICE → auto-dismissed; CAPTCHA/SSO/LOGIN/BLOCKED → escalated to `MANUAL_ACTION_REQUIRED` with the live VNC viewer.

## Operational notes

- **What if the join fails?** Make sure your stored Zoom account has signed in from a normal browser at least once, then try again. If the failure persists, try removing the credentials and re-saving them — the persistent profile is wiped on credential removal, which clears any stale cookies.
- **AWS egress IP reputation** is the residual factor we cannot fully control from inside the container. If many users in your org see joins fail even when signed in, route VP egress through a NAT or residential-proxy provider.
- **Disabling AI fallback**: set the task-definition env var `BEDROCK_DOM_RESOLVER_MODEL_ID=""` and the resolver becomes a no-op (returns `null`); behavior reverts to the pre-change hardcoded path.
