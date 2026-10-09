/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The signed-in user's display name, read from their Cognito id token.
 *
 * This is the fallback for the panel's "Your name" field. The meeting readers
 * scrape it from the meeting page, but a page that has changed shape, a lobby
 * the user has not yet been admitted from, or a platform with no reader at all
 * leaves it empty — and the name is required before transcription can start, so
 * the user would otherwise have to type it every time.
 *
 * Nothing here is a security check. The token is only read for a label to show
 * back to the user who is already holding it; the signature is not verified and
 * must not be relied on. The access token, not this, is what authenticates the
 * WebSocket connection.
 */

/** Claims we are willing to read a display name out of, best first. */
const NAME_CLAIMS = ['name', 'preferred_username', 'given_name', 'email', 'cognito:username'];

/** A bare UUID, which is what `sub` and some usernames are — not a name. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Decode the claims of a JWT without verifying it.
 *
 * Returns `{}` for anything that is not a three-part token with a decodable
 * JSON payload, so a malformed or absent token is simply "no name available".
 */
function claimsOf(jwt: string): { [claim: string]: unknown } {
  const parts = jwt.split('.');
  if (parts.length !== 3) {
    return {};
  }
  try {
    // JWTs are base64url: restore the base64 alphabet and the padding `atob` wants.
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
    // `atob` yields one character per decoded *byte*, so the claims have to be
    // read back through a UTF-8 decoder. Taking its output as a string instead
    // would turn every accented or non-Latin character of someone's name into
    // mojibake, and that name labels their speech in the transcript.
    const bytes = Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
    const claims = JSON.parse(new TextDecoder().decode(bytes));
    return (claims && typeof claims === 'object') ? claims : {};
  } catch (error) {
    // Logged without the error, which would carry token material into the console.
    console.log('Unable to read claims from id token');
    return {};
  }
}

/**
 * Name the signed-in user, or return '' if the token does not say who they are.
 *
 * `given_name` is joined with `family_name` when both are present. An email
 * address is reduced to its local part, since "Your name" is shown to other
 * meeting participants in the transcript and a full address reads badly there.
 */
export function nameFromIdToken(idToken: string | undefined | null): string {
  if (!idToken) {
    return '';
  }
  const claims = claimsOf(idToken);

  // `name` first, as NAME_CLAIMS says: a federated pool often maps all three,
  // and the identity provider's single `name` is the one the user recognises.
  // given+family is the fallback for pools that only populate the parts.
  const name = typeof claims.name === 'string' ? claims.name.trim() : '';
  if (name) {
    return name;
  }
  const given = typeof claims.given_name === 'string' ? claims.given_name.trim() : '';
  const family = typeof claims.family_name === 'string' ? claims.family_name.trim() : '';
  if (given && family) {
    return `${given} ${family}`;
  }

  for (let i = 0; i < NAME_CLAIMS.length; i += 1) {
    const value = claims[NAME_CLAIMS[i]];
    if (typeof value !== 'string') {
      continue;
    }
    const candidate = (NAME_CLAIMS[i] === 'email' ? value.split('@')[0] : value).trim();
    if (candidate && !UUID.test(candidate)) {
      return candidate;
    }
  }
  return '';
}
