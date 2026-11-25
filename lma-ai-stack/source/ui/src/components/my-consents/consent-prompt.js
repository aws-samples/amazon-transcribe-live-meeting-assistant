/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The prompt a Participant sees on signing in, before they have any consent
 * given in advance on record: they land on My consents, with a banner that
 * says what they can record there and a "Not now" that only dismisses it.
 *
 * The dismissal is kept in this browser's local storage, under the person's
 * Cognito sub: it is a display preference, not a consent decision, so nothing
 * is recorded about it on the server, and another person signing in on the
 * same browser is asked for themselves. See
 * docs/meeting-consent.md#consent-given-in-advance.
 */

export const CONSENT_PROMPT_TEXT =
  'Meetings recorded with Live Meeting Assistant ask everyone for consent. You can record your choice here to ' +
  'avoid being asked in every meeting — for one meeting, for meetings with an organizer, or for all meetings on ' +
  'this deployment. You can change or withdraw it at any time, and you can still object in any meeting.';

const storageKey = (sub) => `lma.consentPrompt.dismissed.${sub}`;

/** Whether this person dismissed the prompt in this browser. */
export const promptDismissed = (sub, storage = globalThis.localStorage) => {
  if (!sub) return true;
  try {
    return storage?.getItem(storageKey(sub)) === 'true';
  } catch (e) {
    return false;
  }
};

/** Remember, in this browser, that this person dismissed the prompt. */
export const dismissPrompt = (sub, storage = globalThis.localStorage) => {
  if (!sub) return;
  try {
    storage?.setItem(storageKey(sub), 'true');
  } catch (e) {
    // Storage unavailable (private mode): the banner is dismissed for this visit only.
  }
};

/**
 * Whether to show the prompt: the deployment takes consent given in advance,
 * the person has none on record (in force or not), and has not dismissed it.
 * `answer` is listMyAdvanceConsents' answer.
 */
export const shouldPrompt = (answer, sub, storage = globalThis.localStorage) =>
  Boolean(answer?.Enabled) && (answer?.Consents || []).length === 0 && !promptDismissed(sub, storage);

/** The person's Cognito sub, from the session the app holds. */
export const sessionSub = (appContext) => appContext?.currentSession?.tokens?.idToken?.payload?.sub || '';
