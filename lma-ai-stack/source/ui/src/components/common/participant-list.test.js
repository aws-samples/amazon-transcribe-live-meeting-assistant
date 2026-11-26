/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  MAX_PARTICIPANTS,
  accountCreationSummary,
  accountLabel,
  parseParticipantEntry,
  parseParticipantsText,
  participantsToText,
  sameParticipants,
  searchableText,
  withParticipant,
} from './participant-list';

describe('withParticipant', () => {
  it('adds a person picked from the accounts at the end of the list as Name <email>', () => {
    expect(withParticipant('ana@example.com\n', { name: 'Bo Chen', email: 'bo@example.com' })).toBe(
      'ana@example.com\nBo Chen <bo@example.com>',
    );
    expect(withParticipant('', { name: '', email: 'bo@example.com' })).toBe('bo@example.com');
  });

  it('leaves the list as it is when the address is already on it, whatever its case', () => {
    const text = 'Bo <BO@example.com>';
    expect(withParticipant(text, { name: 'Bo Chen', email: 'bo@example.com' })).toBe(text);
  });

  it('keeps a picked name from breaking the entry it is written into', () => {
    const text = withParticipant('', { name: 'Bo <x> Chen', email: 'bo@example.com' });
    expect(parseParticipantsText(text).participants).toEqual([{ name: 'Bo x Chen', email: 'bo@example.com' }]);
  });
});

describe('searchableText', () => {
  it('needs three characters, not counting spacing at either end', () => {
    expect(searchableText('ro')).toBe(false);
    expect(searchableText('  ro  ')).toBe(false);
    expect(searchableText('rob')).toBe(true);
  });
});

describe('parseParticipantsText', () => {
  it('reads Name <email>, quoted names, "Name email" and bare addresses', () => {
    expect(parseParticipantEntry('Ana Lima <ana@example.com>')).toEqual({ name: 'Ana Lima', email: 'ana@example.com' });
    expect(parseParticipantEntry('"Lima, Ana" <ana@example.com>')).toEqual({
      name: 'Lima, Ana',
      email: 'ana@example.com',
    });
    expect(parseParticipantEntry('Bo Day bo@example.org')).toEqual({ name: 'Bo Day', email: 'bo@example.org' });
    expect(parseParticipantEntry('mailto:cy@example.net')).toEqual({ name: '', email: 'cy@example.net' });
  });

  it('splits at line breaks, semicolons and commas, but not a comma inside a name', () => {
    const { participants, errors } = parseParticipantsText(
      'Lima, Ana <ana@example.com>, bo@example.org; Cy <cy@example.net>\ndi@example.com',
    );
    expect(errors).toEqual([]);
    expect(participants).toEqual([
      { name: 'Lima, Ana', email: 'ana@example.com' },
      { name: '', email: 'bo@example.org' },
      { name: 'Cy', email: 'cy@example.net' },
      { name: '', email: 'di@example.com' },
    ]);
  });

  it('collapses runs of white space in a name and skips blank lines', () => {
    expect(parseParticipantsText('\n  Ana    Lima   <ana@example.com>  \n\n').participants).toEqual([
      { name: 'Ana Lima', email: 'ana@example.com' },
    ]);
  });

  it('keeps an address once, ignoring case, at its first place with the first name given', () => {
    const { participants } = parseParticipantsText(
      'Ana@Example.com\nAna Lima <ana@example.com>\nOther <ANA@EXAMPLE.COM>\nBo <bo@example.org>',
    );
    expect(participants).toEqual([
      { name: 'Ana Lima', email: 'Ana@Example.com' },
      { name: 'Bo', email: 'bo@example.org' },
    ]);
  });

  it('reports an entry without a valid address and keeps the rest', () => {
    const { participants, errors } = parseParticipantsText('Ana <ana@example>\nbo@example.org\njust a name');
    expect(participants).toEqual([{ name: '', email: 'bo@example.org' }]);
    expect(errors).toHaveLength(2);
    expect(errors[0]).toContain('no valid email address');
  });

  it('refuses an over-long name or address', () => {
    expect(parseParticipantsText(`${'n'.repeat(201)} <ana@example.com>`).errors[0]).toContain('200');
    expect(parseParticipantsText(`${'a'.repeat(250)}@example.com`).errors[0]).toContain('no valid email');
  });

  it(`refuses more than ${MAX_PARTICIPANTS} people, not counting repeats`, () => {
    const full = Array.from({ length: MAX_PARTICIPANTS }, (_, i) => `p${i}@example.com`);
    expect(parseParticipantsText([...full, 'P0@example.com'].join('\n')).errors).toEqual([]);
    expect(parseParticipantsText([...full, 'one-more@example.com'].join('\n')).errors[0]).toContain(
      `at most ${MAX_PARTICIPANTS}`,
    );
  });

  it('formats a list back to the text it parses from', () => {
    const list = [
      { name: 'Ana Lima', email: 'ana@example.com' },
      { name: '', email: 'bo@example.org' },
    ];
    const text = participantsToText(list);
    expect(text).toBe('Ana Lima <ana@example.com>\nbo@example.org');
    expect(sameParticipants(parseParticipantsText(text).participants, list)).toBe(true);
  });
});

describe('account answers', () => {
  it('names each account answer', () => {
    expect(accountLabel('none')).toBe('No account');
    expect(accountLabel('Participant')).toBe('Participant');
    expect(accountLabel('Admin')).toBe('Admin');
  });

  it('summarises every outcome of a creation', () => {
    const summary = accountCreationSummary({
      results: [
        { email: 'a@example.com', outcome: 'created' },
        { email: 'b@example.com', outcome: 'exists' },
        { email: 'c@other.org', outcome: 'domain_not_allowed' },
        { email: 'd@example.com', outcome: 'limit_reached', message: 'at most 25 a day' },
        { email: 'e@example.com', outcome: 'failed', message: 'The account could not be created' },
      ],
      created: 1,
      remainingToday: 0,
    });
    expect(summary.type).toBe('warning');
    expect(summary.text).toContain('Created 1 Participant account');
    expect(summary.text).toContain('1 person already has an account');
    expect(summary.text).toContain('c@other.org');
    expect(summary.text).toContain('d@example.com (at most 25 a day)');
    expect(summary.text).toContain('e@example.com (The account could not be created)');
    expect(summary.text).toContain('0 more today');
    expect(accountCreationSummary({ results: [{ email: 'a@x.com', outcome: 'created' }] }).type).toBe('success');
  });
});
