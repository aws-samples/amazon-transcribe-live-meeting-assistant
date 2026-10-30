/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How many connections one open viewer makes.
 *
 * One viewer session must hold one connection: a re-render, an update to the
 * VP record (status, manual-action fields), or a View Only / Scale to Fit
 * toggle must not mint a new token or open a new connection, and a connection
 * the viewer itself closed must not schedule a reconnect. noVNC is replaced by
 * a fake whose disconnect() reports `disconnect` asynchronously, as noVNC 1.7
 * does (after the WebSocket's close event).
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, fireEvent, screen } from '@testing-library/react';

const graphql = vi.hoisted(() => vi.fn());
const instances = vi.hoisted(() => []);
// When set, every new client is accepted by the server shortly after it is created.
const server = vi.hoisted(() => ({ autoAccept: false }));

// Round-trip times that match a live MicroVM viewer: minting a token through
// AppSync takes longer than closing a WebSocket, so the old client's disconnect
// event arrives while the next token is still being minted.
const MINT_MS = vi.hoisted(() => 150);
const CLOSE_MS = vi.hoisted(() => 20);

vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({ graphql }),
}));

vi.mock('@novnc/novnc', () => {
  class FakeRFB extends EventTarget {
    constructor(target, url, options) {
      super();
      this.target = target;
      this.url = url;
      this.options = options;
      this.disconnected = false;
      this.viewOnly = false;
      this.scaleViewport = false;
      instances.push(this);
      if (server.autoAccept) {
        setTimeout(() => this.serverAccepts(), 10);
      }
    }

    // Test helpers: what the server or network would do.
    serverAccepts() {
      this.dispatchEvent(new CustomEvent('connect', { detail: {} }));
    }

    serverDrops() {
      this.dispatchEvent(new CustomEvent('disconnect', { detail: { clean: false } }));
    }

    disconnect() {
      this.disconnected = true;
      setTimeout(() => {
        this.dispatchEvent(new CustomEvent('disconnect', { detail: { clean: true } }));
      }, CLOSE_MS);
    }

    sendCtrlAltDel() {} // eslint-disable-line class-methods-use-this
  }
  return { default: FakeRFB };
});

// eslint-disable-next-line import/first
import VNCViewer from './VNCViewer';

const MVM_ENDPOINT = 'wss://a1b2c3d4.lambda-microvm.us-west-2.on.aws';
const CF_ENDPOINT = 'wss://d123abc.cloudfront.net/vnc/vp-1';

const vpProps = (overrides = {}) => ({
  vpId: 'vp-1',
  vncEndpoint: MVM_ENDPOINT,
  status: 'JOINING',
  ...overrides,
});

// Advance fake time in small steps, each inside its own act(), so React
// commits the state changes a timer makes (a reconnect is a state change)
// before the next timer is due, as it would in a browser.
// One viewer as a page renders it from a VP record.
const viewer = (overrides = {}) => {
  const p = vpProps(overrides);
  return (
    <VNCViewer
      vpId={p.vpId}
      vncEndpoint={p.vncEndpoint}
      status={p.status}
      manualActionType={p.manualActionType}
      manualActionMessage={p.manualActionMessage}
      manualActionTimeoutSeconds={p.manualActionTimeoutSeconds}
      manualActionStartTime={p.manualActionStartTime}
    />
  );
};

const flush = async (ms = 0) => {
  const step = 10;
  let remaining = ms;
  do {
    const advance = Math.min(step, remaining);
    // eslint-disable-next-line no-await-in-loop
    await act(async () => {
      await vi.advanceTimersByTimeAsync(advance);
    });
    remaining -= advance;
  } while (remaining > 0);
};

const mintCount = () => graphql.mock.calls.length;

beforeEach(() => {
  vi.useFakeTimers();
  instances.length = 0;
  server.autoAccept = false;
  graphql.mockReset();
  graphql.mockImplementation(async ({ query }) => {
    await new Promise((resolve) => {
      setTimeout(resolve, MINT_MS);
    });
    return query.includes('createMicrovmVncToken')
      ? { data: { createMicrovmVncToken: { authToken: 'jwe', expiresAt: '2026-10-30T21:00:00Z' } } }
      : { data: { createVncEdgeToken: { token: 'edge', expiresAt: '2026-10-30T21:00:00Z', vncPassword: 'pw' } } };
  });
  vi.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('VNCViewer connection lifecycle', () => {
  it('keeps one connection across VP record updates, including a manual-action prompt', async () => {
    server.autoAccept = true;
    const { rerender } = render(viewer());
    await flush(MINT_MS + 50);
    expect(instances).toHaveLength(1);

    // The VP subscription delivers a fresh object on every update; the
    // manual-action prompt also switches View Only off.
    rerender(
      viewer({
        status: 'MANUAL_ACTION_REQUIRED',
        manualActionType: 'LOGIN',
        manualActionMessage: 'Enter the passcode',
        manualActionTimeoutSeconds: 300,
        manualActionStartTime: new Date().toISOString(),
      }),
    );
    await flush(30000);
    rerender(viewer({ status: 'JOINED' }));
    await flush(30000);

    expect(mintCount()).toBe(1);
    expect(instances).toHaveLength(1);
    expect(instances[0].disconnected).toBe(false);
    // View Only was switched off on the live client rather than by reconnecting.
    expect(instances[0].viewOnly).toBe(false);
  });

  it('applies the View Only toggle to the live client without reconnecting', async () => {
    server.autoAccept = true;
    render(viewer());
    await flush(MINT_MS + 50);
    expect(instances[0].viewOnly).toBe(true);

    fireEvent.click(screen.getByRole('checkbox', { name: /View Only/ }));
    await flush(30000);

    expect(instances).toHaveLength(1);
    expect(instances[0].viewOnly).toBe(false);
    expect(mintCount()).toBe(1);
  });

  it('does not reconnect again after closing a connection itself', async () => {
    render(viewer({ vncEndpoint: CF_ENDPOINT }));
    await flush(MINT_MS);
    instances[0].serverAccepts();
    await flush(MINT_MS);

    fireEvent.click(screen.getByRole('button', { name: /Reconnect/ }));
    await flush(MINT_MS);
    expect(instances).toHaveLength(2);
    instances[1].serverAccepts();

    // The first client's disconnect event arrives after the second has started.
    await flush(30000);

    expect(instances[0].disconnected).toBe(true);
    expect(instances).toHaveLength(2);
    expect(instances[1].disconnected).toBe(false);
    expect(mintCount()).toBe(2);
  });

  it('does not connect when unmounted while the token is being minted', async () => {
    let resolveMint;
    graphql.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveMint = resolve;
        }),
    );
    const { unmount } = render(viewer());
    unmount();
    resolveMint({ data: { createMicrovmVncToken: { authToken: 'jwe', expiresAt: '2026-10-30T21:00:00Z' } } });
    await flush(30000);

    expect(instances).toHaveLength(0);
  });

  it('reconnects after a real drop, with a growing delay when connections keep dropping', async () => {
    render(viewer());
    await flush(MINT_MS);
    instances[0].serverAccepts();
    await flush(0);

    // First drop: one reconnect (one new token) after 1 s.
    instances[0].serverDrops();
    await flush(999);
    expect(mintCount()).toBe(1);
    await flush(1);
    expect(mintCount()).toBe(2);
    await flush(MINT_MS);
    expect(instances).toHaveLength(2);

    // The new connection is accepted and dropped at once: the backoff keeps
    // growing (2 s) instead of starting over at 1 s.
    instances[1].serverAccepts();
    instances[1].serverDrops();
    await flush(1999);
    expect(mintCount()).toBe(2);
    await flush(1);
    expect(mintCount()).toBe(3);
  });

  it('starts the backoff over after a connection that held', async () => {
    render(viewer());
    await flush(MINT_MS);
    instances[0].serverAccepts();
    instances[0].serverDrops();
    await flush(1000 + MINT_MS);
    expect(instances).toHaveLength(2);

    instances[1].serverAccepts();
    await flush(60000);
    instances[1].serverDrops();
    await flush(1000);
    expect(mintCount()).toBe(3);
  });
});
