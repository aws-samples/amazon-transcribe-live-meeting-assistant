---
title: Zoom Sign-in & Bot-Detection Hardening
sidebar_label: Zoom Sign-in
---

# Zoom Sign-in & Bot-Detection Hardening

LMA's Virtual Participant (VP) joins Zoom meetings via headless Chromium. Zoom's bot-detection sometimes blocks guest joins with the dialog *"We detected you may be a bot. Automated bots aren't allowed to join this meeting or webinar..."*. This page describes the features LMA ships to mitigate that, and how to use them.

## Browser stack

- **`rebrowser-puppeteer`** (replaces `puppeteer-extra-plugin-stealth`) patches CDP at the runtime-binding layer so pages can't detect the `Runtime.Evaluate` / `chrome.runtime.connect` shims that the Stealth plugin had to add on top of vanilla Puppeteer. Strictly fewer fingerprint signals than the previous Stealth-plugin posture.
- **Chromium on Alpine** (apk-installed) is the base browser, not Chrome Stable. Combined with the persistent S3 profile (which carries the trusted-device cookies), the marginal benefit of Chrome-vs-Chromium is small enough that the CVE-noise reduction (`node:22-alpine` has materially fewer findings than `node:22-bookworm-slim`) wins.
- **`ghost-cursor`** drives a Bezier-curve mouse path on the cursor-pathing-sensitive clicks (Sign-In, Join, "Skip for now" interstitials, in-meeting chat-panel button). Audio/video SVG toggles still use a direct ancestor-walk click since SVG elements can't be the cursor target directly.

## Per-user Zoom credentials

Each LMA user can store **one** set of Zoom credentials. When a user starts a virtual participant for a Zoom meeting and ticks **"Sign in with my stored Zoom account when joining this meeting"**, the VP signs in to Zoom before navigating to the meeting URL. A signed-in session avoids most bot-detection blocks and allows the VP to join meetings that disallow guests.

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

- **CAPTCHA / 2FA still happens.** When Zoom challenges the sign-in, the VP escalates to `MANUAL_ACTION_REQUIRED` and the React UI surfaces a Flashbar alert + the live noVNC viewer. Solve the challenge there; the VP picks up automatically when the session is authenticated.
- **Brand-new accounts can still look bot-shaped.** Sign in to Zoom on your own laptop with the account at least once *before* relying on LMA — accounts whose only activity is joining meetings from AWS IP ranges can still trip detection.

## AI-driven sign-in loop

Zoom's sign-in flow is not a single page — after the password is accepted, the user typically sees a sequence of post-login interstitials (passkey-binding upsell, phone-binding upsell, "verify your email" notices, OTP entry, regional-disclosure consent, etc.) before landing on the dashboard. The set and order of these pages changes over time and per-account.

The VP delegates each post-username step to Claude (Bedrock, vision-capable). After submitting the username, the sign-in driver enters a loop:

1. Take a screenshot + a compact DOM summary of the current page.
2. Ask Claude what to do next. Claude returns one of: **fill_password** (selector + reason), **skip** (selector + reason), **continue** (the page is loading, just wait), **wait**, **needs_human** (escalate to MANUAL_ACTION_REQUIRED), or **done** (we're authenticated).
3. Execute the action; loop.

This keeps the deterministic logic out of the codebase and lets the VP tolerate Zoom's frequent sign-in flow changes without a code/deploy cycle.

The same AI navigator handles the post-login interstitial sequence on the way to the meeting URL, and the in-meeting unknown-dialog watchdog (consent, recording-notice, bot-detection, etc.).

## Persistent Chromium profile per user (Phase C4)

The VP container hydrates a per-user `userDataDir` from S3 at launch and uploads it back at meeting end. Once the user has signed in once and solved any reCAPTCHA / 2FA via the VNC viewer, Zoom plants a "trusted device" cookie that persists. Subsequent meetings reuse that cookie and skip both the reCAPTCHA *and* the bot-detection dialog.

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

- **What if I see "Zoom blocked the join"?** Make sure your stored Zoom account has signed in from a normal browser at least once, then try again. If the failure persists, try removing the credentials and re-saving them — the persistent profile is wiped on credential removal, which clears any stale cookies.
- **AWS egress IP reputation** is the residual risk we cannot fully mitigate from inside the container. If many users in your org see blocks even when signed in, route VP egress through a NAT or residential-proxy provider.
- **Disabling AI fallback**: set the task-definition env var `BEDROCK_DOM_RESOLVER_MODEL_ID=""` and the resolver becomes a no-op (returns `null`); behavior reverts to the pre-change hardcoded path.
