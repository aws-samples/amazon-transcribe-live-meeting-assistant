/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Consent given in advance, in the web UI: the meeting's consent link, the
 * invitation the organizer sends from their own mail client, and how a stored
 * consent is described. See docs/meeting-consent.md#consent-given-in-advance.
 */
import { MY_CONSENTS_PATH } from '../../routes/constants';

/** The most characters a mailto: link is given; longer ones are cut by some mail clients. */
export const MAILTO_LIMIT = 1900;

/**
 * The meeting's consent link: the My consents page with the meeting named, by
 * CallId or, before a Virtual Participant has created its meeting, by the
 * Virtual Participant's id. `base` is the web UI's address (origin and path).
 */
export const consentLink = (base, { callId = '', virtualParticipantId = '' } = {}) => {
  const query = callId ? `meeting=${encodeURIComponent(callId)}` : `vp=${encodeURIComponent(virtualParticipantId)}`;
  return `${String(base || '').replace(/[/#]+$/, '')}/#${MY_CONSENTS_PATH}?${query}`;
};

/** The web UI's own address, as a consent link starts with it. */
export const appBase = (location = window.location) => `${location.origin}${location.pathname.replace(/[^/]*$/, '')}`;

/**
 * The meeting a consent link names, from the page's query string: the
 * arguments getAdvanceConsentInvitation takes, or null when it names none.
 */
export const consentLinkMeeting = (search = '') => {
  const params = new URLSearchParams(search);
  const callId = params.get('meeting');
  if (callId) return { CallId: callId };
  const vpId = params.get('vp');
  if (vpId) return { VirtualParticipantId: vpId };
  return null;
};

const formatWhen = (iso) => {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toUTCString().replace(' GMT', ' UTC');
};

/**
 * The one email the organizer sends to everyone on the list: the recipients,
 * the subject, the text, a mailto: link, and whether the link had to be
 * shortened (then the recipients are left out of it, and the text is to be
 * copied instead).
 */
export const consentInvitation = ({ meetingName = '', organizer = '', scheduledFor = '', link, participants = [] }) => {
  const to = participants.map((p) => p.email).filter(Boolean);
  const name = meetingName || 'our meeting';
  const when = formatWhen(scheduledFor);
  const subject = `Consent to recording: ${name}`;
  const body = [
    'Hello,',
    '',
    `I would like to record and transcribe ${name}${when ? ` (${when})` : ''} with Live Meeting Assistant. ` +
      'You can say whether you agree before the meeting, on this page, after signing in with the email address ' +
      'this message was sent to:',
    '',
    link,
    '',
    'You can give consent for this meeting only, or for the meetings I organize up to a date you choose, and ' +
      'you can withdraw it at any time on the same page. Giving consent in advance is optional.',
    '',
    'If you do not have an account yet, reply to this message and I will create one for you.',
    '',
    organizer ? `Thanks,\n${organizer}` : 'Thanks',
  ].join('\n');
  const encode = (value) => encodeURIComponent(value);
  let mailto = `mailto:${to.map(encode).join(',')}?subject=${encode(subject)}&body=${encode(body)}`;
  let shortened = false;
  if (mailto.length > MAILTO_LIMIT) {
    shortened = true;
    mailto = `mailto:?subject=${encode(subject)}&body=${encode(body)}`;
  }
  return { to, subject, body, mailto, shortened, text: `To: ${to.join(', ')}\nSubject: ${subject}\n\n${body}` };
};

/** What a consent covers, in words, for the My consents page. */
export const consentScopeLabel = (consent) => {
  if (!consent) return '';
  if (consent.Scope === 'meeting') return `This meeting: ${consent.MeetingName || consent.CallId || ''}`.trim();
  if (consent.Scope === 'deployment') return 'All meetings on this deployment';
  const label = consent.LabelKey ? `, labelled ${consent.LabelKey}: ${consent.LabelValue || ''}` : '';
  return `Meetings organized by ${consent.Organizer || 'the organizer'}${label}`;
};

/** The status of a consent, as the page labels it. */
export const consentStatusLabel = (consent) => {
  if (consent?.Status === 'active') return 'In force';
  if (consent?.Status === 'expired') return 'Expired';
  if (consent?.WithdrawnReason === 'replaced') return 'Replaced';
  if (consent?.WithdrawnReason === 'account_deleted') return 'Withdrawn (account deleted)';
  if (consent?.WithdrawnReason === 'withdrawn_by_admin') return 'Withdrawn by an administrator';
  return 'Withdrawn';
};

/** A local calendar day as YYYY-MM-DD: a date picker's days are the browser's own. */
export const localDay = (date) =>
  `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;

/** A date `days` ahead, written YYYY-MM-DD in UTC, as the API takes ValidUntil. */
export const dateAhead = (days, now = new Date()) => {
  const date = new Date(now.getTime() + days * 86400000);
  return date.toISOString().slice(0, 10);
};
