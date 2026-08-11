// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, it, expect, vi } from 'vitest';
import { buildVncConnection, isMicrovmEndpoint, fetchMicrovmAuthToken, MICROVM_VNC_PORT } from './vncConnection';

const CF_ENDPOINT = 'wss://d123abc.cloudfront.net/vnc/vp-123';
const MVM_ENDPOINT = 'wss://a1b2c3d4.lambda-microvm.us-west-2.on.aws';

describe('isMicrovmEndpoint', () => {
  it('recognises Lambda MicroVM endpoints', () => {
    expect(isMicrovmEndpoint(MVM_ENDPOINT)).toBe(true);
    expect(isMicrovmEndpoint('wss://x.lambda-microvm.eu-west-1.on.aws')).toBe(true);
  });

  it('treats CloudFront/ALB endpoints as the ECS transport', () => {
    expect(isMicrovmEndpoint(CF_ENDPOINT)).toBe(false);
    expect(isMicrovmEndpoint('wss://example.com/vnc/vp-1')).toBe(false);
  });

  it('is safe with missing or non-string input', () => {
    expect(isMicrovmEndpoint(undefined)).toBe(false);
    expect(isMicrovmEndpoint(null)).toBe(false);
    expect(isMicrovmEndpoint('')).toBe(false);
    expect(isMicrovmEndpoint(42)).toBe(false);
  });
});

describe('buildVncConnection — ECS (CloudFront/ALB) transport', () => {
  it('appends the Cognito ID token as a query parameter', () => {
    const { url, wsProtocols } = buildVncConnection({
      endpoint: CF_ENDPOINT,
      idToken: 'id-token-value',
    });
    expect(url).toContain('token=id-token-value');
    expect(url.startsWith('wss://d123abc.cloudfront.net/vnc/vp-123')).toBe(true);
    // No subprotocols on this transport: websockify would reject unknown ones.
    expect(wsProtocols).toEqual([]);
  });

  it('throws when the Cognito token is missing', () => {
    expect(() => buildVncConnection({ endpoint: CF_ENDPOINT })).toThrow(/Cognito ID token/);
  });

  it('preserves the vpId path used for multi-user routing', () => {
    const { url } = buildVncConnection({ endpoint: CF_ENDPOINT, idToken: 't' });
    expect(url).toContain('/vnc/vp-123');
  });
});

describe('buildVncConnection — Lambda MicroVMs transport', () => {
  it('passes the auth token and target port as WebSocket subprotocols', () => {
    // Browsers cannot set the X-aws-proxy-auth header on a WebSocket, so Lambda
    // accepts the token as a subprotocol instead. noVNC 1.7 forwards
    // options.wsProtocols to `new WebSocket(url, protocols)`.
    const { url, wsProtocols } = buildVncConnection({
      endpoint: MVM_ENDPOINT,
      authToken: 'jwe-token',
    });
    expect(url).toBe(MVM_ENDPOINT);
    expect(wsProtocols).toEqual([
      'lambda-microvms',
      'lambda-microvms.authentication.jwe-token',
      `lambda-microvms.port.${MICROVM_VNC_PORT}`,
    ]);
  });

  it('targets port 5901, where websockify serves noVNC', () => {
    expect(MICROVM_VNC_PORT).toBe(5901);
    const { wsProtocols } = buildVncConnection({ endpoint: MVM_ENDPOINT, authToken: 't' });
    expect(wsProtocols).toContain('lambda-microvms.port.5901');
  });

  it('never puts the auth token in the URL', () => {
    // Tokens in URLs get captured by proxy/access logs far more readily than
    // subprotocol values.
    const { url } = buildVncConnection({ endpoint: MVM_ENDPOINT, authToken: 'secret-jwe' });
    expect(url).not.toContain('secret-jwe');
    expect(url).not.toContain('token=');
  });

  it('throws a specific error when the MicroVM token is missing', () => {
    // Without this the MicroVM endpoint returns an opaque 403.
    expect(() => buildVncConnection({ endpoint: MVM_ENDPOINT })).toThrow(/MicroVM auth token/);
  });

  it('does not require a Cognito token on this transport', () => {
    expect(() => buildVncConnection({ endpoint: MVM_ENDPOINT, authToken: 'jwe' })).not.toThrow();
  });
});

describe('buildVncConnection — input validation', () => {
  it('throws when no endpoint is supplied', () => {
    expect(() => buildVncConnection({})).toThrow(/No VNC endpoint/);
    expect(() => buildVncConnection({ endpoint: '' })).toThrow(/No VNC endpoint/);
  });
});

describe('fetchMicrovmAuthToken', () => {
  it('returns the token from the mutation response', async () => {
    const client = {
      graphql: vi.fn().mockResolvedValue({
        data: { createMicrovmVncToken: { authToken: 'jwe-abc', expiresAt: '2026-08-07T20:00:00Z' } },
      }),
    };
    await expect(fetchMicrovmAuthToken(client, 'vp-1')).resolves.toBe('jwe-abc');
    expect(client.graphql).toHaveBeenCalledWith(expect.objectContaining({ variables: { vpId: 'vp-1' } }));
  });

  it('throws when the resolver returns no token', async () => {
    // Better than handing `undefined` to the WebSocket and getting an opaque 403.
    const client = { graphql: vi.fn().mockResolvedValue({ data: { createMicrovmVncToken: null } }) };
    await expect(fetchMicrovmAuthToken(client, 'vp-1')).rejects.toThrow(/could not mint/i);
  });

  it('propagates transport errors', async () => {
    const client = { graphql: vi.fn().mockRejectedValue(new Error('network down')) };
    await expect(fetchMicrovmAuthToken(client, 'vp-1')).rejects.toThrow(/network down/);
  });
});
