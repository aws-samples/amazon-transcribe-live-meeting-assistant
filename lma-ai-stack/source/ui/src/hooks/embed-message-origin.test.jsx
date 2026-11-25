/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The origins an embed accepts messages from, and token passing's use of them:
 * the allowedOrigins query parameter's, or the embed's own origin.
 */
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { embedMessageOrigins, isAcceptedEmbedMessage } from './embed-message-origin';
import usePostMessageAuth from './use-postmessage-auth';

const OWN = window.location.origin;
const CRM = 'https://crm.example.com';
const OTHER = 'https://other.example.com';

const message = (origin, data) => new MessageEvent('message', { origin, data });
const tokens = { type: 'LMA_AUTH', idToken: 'id', accessToken: 'access', refreshToken: 'refresh' };

describe('embedMessageOrigins', () => {
  it('is the embed own origin when allowedOrigins is empty', () => {
    expect(embedMessageOrigins([])).toEqual([OWN]);
    expect(embedMessageOrigins()).toEqual([OWN]);
  });

  it('is allowedOrigins when it is given', () => {
    expect(embedMessageOrigins([CRM, OTHER])).toEqual([CRM, OTHER]);
  });
});

describe('isAcceptedEmbedMessage', () => {
  it('accepts only the embed own origin when allowedOrigins is empty', () => {
    expect(isAcceptedEmbedMessage(message(OWN, {}), [])).toBe(true);
    expect(isAcceptedEmbedMessage(message(CRM, {}), [])).toBe(false);
    expect(isAcceptedEmbedMessage(message('', {}), [])).toBe(false);
  });

  it('accepts only the listed origins when allowedOrigins is given', () => {
    expect(isAcceptedEmbedMessage(message(CRM, {}), [CRM])).toBe(true);
    expect(isAcceptedEmbedMessage(message(OWN, {}), [CRM])).toBe(false);
    expect(isAcceptedEmbedMessage(message(OTHER, {}), [CRM])).toBe(false);
  });
});

describe('usePostMessageAuth', () => {
  const render = (allowedOrigins) => renderHook(() => usePostMessageAuth({ enabled: true, allowedOrigins }));

  it('takes tokens only from the embed own origin when allowedOrigins is empty', () => {
    const { result } = render([]);
    act(() => window.dispatchEvent(message(CRM, tokens)));
    expect(result.current.isAuthenticated).toBe(false);
    act(() => window.dispatchEvent(message(OWN, tokens)));
    expect(result.current.isAuthenticated).toBe(true);
  });

  it('takes tokens only from a listed origin when allowedOrigins is given', () => {
    const { result } = render([CRM]);
    act(() => window.dispatchEvent(message(OTHER, tokens)));
    act(() => window.dispatchEvent(message(OWN, tokens)));
    expect(result.current.isAuthenticated).toBe(false);
    act(() => window.dispatchEvent(message(CRM, tokens)));
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.tokens).toEqual({ idToken: 'id', accessToken: 'access', refreshToken: 'refresh' });
  });
});
