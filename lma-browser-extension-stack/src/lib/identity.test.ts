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

/**
 * An unsigned JWT carrying `claims`, base64url-encoded as a real one would be.
 *
 * The JSON is encoded to UTF-8 bytes before base64, which is what a real token
 * does and what `btoa` alone cannot do — it rejects any code point above U+00FF.
 * Getting this wrong in the helper would make the non-ASCII test vacuous.
 */
function tokenWith(claims: object): string {
  const encode = (value: object) => {
    const bytes = new TextEncoder().encode(JSON.stringify(value));
    let latin1 = '';
    bytes.forEach((byte) => { latin1 += String.fromCharCode(byte); });
    return btoa(latin1).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };
  return `${encode({ alg: 'none' })}.${encode(claims)}.signature`;
}

describe('nameFromIdToken', () => {
  it('prefers the name claim over every other claim', () => {
    // Including given_name/family_name, which a federated pool populates
    // alongside `name`. An earlier version returned the joined parts instead,
    // and this test passed only because it had not supplied them.
    expect(nameFromIdToken(tokenWith({
      name: 'Lovelace, Ada (Contractor)',
      given_name: 'Ada',
      family_name: 'Lovelace',
      preferred_username: 'alovelace',
      email: 'ada@example.com',
      'cognito:username': 'ada',
    }))).toBe('Lovelace, Ada (Contractor)');
  });

  it('reads a name with non-ASCII characters without mangling it', () => {
    // The payload is base64url over UTF-8 bytes, and this name is what appears
    // against the user's speech in the transcript.
    expect(nameFromIdToken(tokenWith({ name: 'José Álvarez' }))).toBe('José Álvarez');
    expect(nameFromIdToken(tokenWith({ given_name: 'Łukasz', family_name: 'Kowalczyk' })))
      .toBe('Łukasz Kowalczyk');
    expect(nameFromIdToken(tokenWith({ name: '田中 太郎' }))).toBe('田中 太郎');
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
