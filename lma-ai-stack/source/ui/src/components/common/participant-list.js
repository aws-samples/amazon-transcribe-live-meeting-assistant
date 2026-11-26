/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * A meeting's participant list as the forms edit it: the names and email
 * addresses of the people expected at the meeting, typed or pasted from a
 * calendar invite.
 *
 * The rules are the server's (normalize_participants in
 * lma_meeting_preparation/rules.py), repeated here so a form can say what is
 * wrong before it is sent; the server checks them again. See
 * docs/virtual-participant.md#participant-list.
 */

export const MAX_PARTICIPANTS = 100;
export const MAX_PARTICIPANT_NAME_CHARS = 200;
export const MAX_PARTICIPANT_EMAIL_CHARS = 254;
export const PARTICIPANT_EMAIL = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

const EMAIL_INSIDE = /[^\s<>,;"]+@[^\s<>,;"]+/;

/**
 * The entries of a pasted list: split at line breaks and ";", and at "," where
 * the comma does not fall inside a name ("Lima, Ana <ana@example.com>"): a
 * piece holding no address is joined to the one after it.
 */
const splitEntries = (text) => {
  const entries = [];
  (text || '').split(/[\r\n;]+/).forEach((chunk) => {
    let pending = '';
    chunk.split(',').forEach((piece) => {
      pending = pending ? `${pending},${piece}` : piece;
      if (EMAIL_INSIDE.test(pending)) {
        entries.push(pending);
        pending = '';
      }
    });
    if (pending.trim()) entries.push(pending);
  });
  return entries.map((entry) => entry.trim()).filter(Boolean);
};

const unquote = (name) => name.replace(/^["'\s]+|["'\s]+$/g, '');
const collapse = (text) => (text || '').split(/\s+/).filter(Boolean).join(' ');

/** One entry as `{ name, email }`: "Name <email>", "\"Name\" <email>", "Name email" or a bare email. */
export const parseParticipantEntry = (entry) => {
  const text = (entry || '').trim();
  const angled = text.match(/^(.*)<([^<>]*)>\s*$/);
  if (angled) return { name: collapse(unquote(angled[1])), email: angled[2].trim().replace(/^mailto:/i, '') };
  const words = text.split(/\s+/);
  const last = words[words.length - 1].replace(/^mailto:/i, '');
  if (words.length > 1 && last.includes('@'))
    return { name: collapse(unquote(words.slice(0, -1).join(' '))), email: last };
  return { name: '', email: text.replace(/^mailto:/i, '') };
};

/**
 * The people in `text`, checked as the server checks them: `{ participants, errors }`.
 * An address given more than once (compared ignoring case) is kept once, at its
 * first place, with the first non-empty name given for it.
 */
export const parseParticipantsText = (text) => {
  const participants = [];
  const byEmail = new Map();
  const errors = [];
  splitEntries(text).forEach((entry) => {
    const { name, email } = parseParticipantEntry(entry);
    if (email.length > MAX_PARTICIPANT_EMAIL_CHARS || !PARTICIPANT_EMAIL.test(email)) {
      errors.push(`"${entry.slice(0, 80)}" has no valid email address`);
      return;
    }
    if (name.length > MAX_PARTICIPANT_NAME_CHARS) {
      errors.push(`The name given for ${email} is longer than ${MAX_PARTICIPANT_NAME_CHARS} characters`);
      return;
    }
    const known = byEmail.get(email.toLowerCase());
    if (known) {
      if (!known.name && name) known.name = name;
      return;
    }
    const participant = { name, email };
    byEmail.set(email.toLowerCase(), participant);
    participants.push(participant);
  });
  if (participants.length > MAX_PARTICIPANTS) errors.push(`A meeting lists at most ${MAX_PARTICIPANTS} participants`);
  return { participants, errors };
};

/** A stored list as the editor shows it: "Name <email>" per line, or the address alone. */
export const participantsToText = (participants) =>
  (participants || [])
    .filter((p) => p && p.email)
    .map(({ name, email }) => (name ? `${name} <${email}>` : email))
    .join('\n');

/** A list as it is sent: `[{ name, email }]` with nothing else. */
export const participantsInput = (participants) =>
  (participants || []).filter((p) => p && p.email).map(({ name, email }) => ({ name: name || '', email }));

/**
 * The editor's text with one more person at the end, as "Name <email>" (or the
 * address alone when the person has no name); unchanged when the address is
 * already in it, compared ignoring case. For the account picker.
 */
export const withParticipant = (text, person) => {
  const email = (person?.email || '').trim();
  if (!email) return text || '';
  const listed = parseParticipantsText(text).participants.some((p) => p.email.toLowerCase() === email.toLowerCase());
  if (listed) return text || '';
  const name = collapse((person.name || '').replace(/[<>]/g, ' '));
  const line = name ? `${name} <${email}>` : email;
  const current = (text || '').replace(/\s+$/, '');
  return current ? `${current}\n${line}` : line;
};

/** The fewest characters the account picker searches with. */
export const MIN_ACCOUNT_SEARCH_CHARS = 3;

/** Whether the typed text is long enough to search accounts with: 3 characters, ignoring spacing. */
export const searchableText = (text) => collapse(text).length >= MIN_ACCOUNT_SEARCH_CHARS;

/** Whether two lists are the same people, names and order. */
export const sameParticipants = (a, b) => JSON.stringify(participantsInput(a)) === JSON.stringify(participantsInput(b));

/** What an account answer reads as in the list. */
export const ACCOUNT_LABELS = {
  none: 'No account',
  Participant: 'Participant',
  User: 'User',
  Admin: 'Admin',
};

export const accountLabel = (account) => ACCOUNT_LABELS[account] || 'Unknown';

/** The summary of a createParticipantAccounts answer, one sentence per outcome. */
export const accountCreationSummary = (answer) => {
  const results = answer?.results || [];
  const count = (outcome) => results.filter((r) => r.outcome === outcome);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const parts = [];
  const created = count('created').length;
  parts.push(`Created ${plural(created, 'Participant account', 'Participant accounts')}; each was sent an invitation.`);
  const exists = count('exists').length;
  if (exists) parts.push(`${plural(exists, 'person already has', 'people already have')} an account.`);
  const domain = count('domain_not_allowed');
  if (domain.length) {
    parts.push(`Not created, outside the allowed email domains: ${domain.map((r) => r.email).join(', ')}.`);
  }
  const limited = count('limit_reached');
  if (limited.length) parts.push(`Not created yet: ${limited.map((r) => r.email).join(', ')} (${limited[0].message}).`);
  const failed = count('failed');
  if (failed.length) {
    parts.push(`Failed: ${failed.map((r) => (r.message ? `${r.email} (${r.message})` : r.email)).join(', ')}.`);
  }
  if (typeof answer?.remainingToday === 'number') {
    parts.push(`You can create ${answer.remainingToday} more today.`);
  }
  return {
    type: failed.length || limited.length || domain.length ? 'warning' : 'success',
    text: parts.join(' '),
  };
};
