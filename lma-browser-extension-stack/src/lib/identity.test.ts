/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Covers the fallback for the panel's "Your name" field.
 *
 * The name is required before transcription can start, and the meeting readers
 * often cannot find it, so this is what keeps the user from retyping it every
 * meeting. The malformed-token cases matter as much as the happy path: this runs
 * on whatever is in extension storage, and a throw here would break the whole
 * capture screen rather than just leaving a field blank.
 */

import { nameFromIdToken } from './identity';

/** An unsigned JWT carrying `claims`, base64url-encoded as a real one would be. */
function tokenWith(claims: object): string {
  const encode = (value: object) => btoa(JSON.stringify(value))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
  return `${encode({ alg: 'none' })}.${encode(claims)}.signature`;
}

describe('nameFromIdToken', () => {
  it('prefers the name claim', () => {
    expect(nameFromIdToken(tokenWith({
      name: 'Ada Lovelace',
      email: 'ada@example.com',
      'cognito:username': 'ada',
    }))).toBe('Ada Lovelace');
  });

  it('joins given and family name when there is no name claim', () => {
    expect(nameFromIdToken(tokenWith({ given_name: 'Ada', family_name: 'Lovelace' })))
      .toBe('Ada Lovelace');
  });

  it('uses the given name alone when there is no family name', () => {
    expect(nameFromIdToken(tokenWith({ given_name: 'Ada' }))).toBe('Ada');
  });

  it('reduces an email address to its local part', () => {
    // "Your name" is shown against this user's speech in the transcript, where a
    // full address reads badly.
    expect(nameFromIdToken(tokenWith({ email: 'ada.lovelace@example.com' })))
      .toBe('ada.lovelace');
  });

  it('skips a username that is a bare UUID', () => {
    // Federated sign-in puts the subject id in `cognito:username`, which is not
    // a name anyone would recognise.
    expect(nameFromIdToken(tokenWith({
      'cognito:username': '7c9e6679-7425-40de-944b-e07fc1f90ae7',
    }))).toBe('');
  });

  it('falls back to the username when it is not a UUID', () => {
    expect(nameFromIdToken(tokenWith({ 'cognito:username': 'alovelace' }))).toBe('alovelace');
  });

  it('ignores whitespace-only claims', () => {
    expect(nameFromIdToken(tokenWith({ name: '   ', email: 'ada@example.com' }))).toBe('ada');
  });

  it('ignores a non-string claim rather than stringifying it', () => {
    expect(nameFromIdToken(tokenWith({ name: 42, email: 'ada@example.com' }))).toBe('ada');
  });

  it.each<[string, string | undefined | null]>([
    ['an empty string', ''],
    ['undefined', undefined],
    ['null', null],
    ['a token with no claims', tokenWith({})],
    ['a string that is not a token', 'not-a-token'],
    ['a token with the wrong number of parts', 'only.two'],
    ['a token whose payload is not base64', 'header.!!!not-base64!!!.signature'],
    ['a token whose payload is not JSON', `header.${btoa('plain text')}.signature`],
  ])('returns an empty name for %s without throwing', (_label, token) => {
    expect(() => nameFromIdToken(token)).not.toThrow();
    expect(nameFromIdToken(token)).toBe('');
  });
});
