/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Where a Participant lands after signing in with no link: My consents only
 * while the deployment takes consent given in advance, they have none on
 * record and they have not dismissed the prompt; otherwise their home page.
 * (A User or an Admin never reaches this: their sign-in follows MemberRoutes.)
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

const { graphql, appContext } = vi.hoisted(() => ({ graphql: vi.fn(), appContext: vi.fn() }));

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    /* eslint-disable class-methods-use-this */
    debug() {}

    warn() {}

    error() {}
    /* eslint-enable class-methods-use-this */
  },
}));
vi.mock('../../contexts/app', () => ({ default: () => appContext() }));

/* eslint-disable import/first */
import SignInLanding from './SignInLanding';
import { dismissPrompt } from './consent-prompt';
/* eslint-enable import/first */

const renderLanding = () =>
  render(
    <MemoryRouter initialEntries={['/login']}>
      <Routes>
        <Route path="/login" element={<SignInLanding />} />
        <Route path="/home" element={<div>home page</div>} />
        <Route path="/consents" element={<div>my consents page</div>} />
      </Routes>
    </MemoryRouter>,
  );

const consents = (answer) => graphql.mockResolvedValue({ data: { listMyAdvanceConsents: answer } });

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  appContext.mockReturnValue({ currentSession: { tokens: { idToken: { payload: { sub: 'sub-bo' } } } } });
});
afterEach(cleanup);

describe('signing in as a Participant with no link', () => {
  it('lands on My consents with no consent on record and the prompt not dismissed', async () => {
    consents({ Enabled: true, Consents: [] });
    renderLanding();
    expect(await screen.findByText('my consents page')).toBeTruthy();
  });

  it('lands on the home page with a consent on record', async () => {
    consents({ Enabled: true, Consents: [{ ConsentId: '1', Status: 'withdrawn' }] });
    renderLanding();
    expect(await screen.findByText('home page')).toBeTruthy();
  });

  it('lands on the home page when the deployment takes no consent given in advance', async () => {
    consents({ Enabled: false, Consents: [] });
    renderLanding();
    expect(await screen.findByText('home page')).toBeTruthy();
  });

  it('lands on the home page once the prompt was dismissed, without reading the consents', async () => {
    dismissPrompt('sub-bo');
    consents({ Enabled: true, Consents: [] });
    renderLanding();
    expect(await screen.findByText('home page')).toBeTruthy();
    expect(graphql).not.toHaveBeenCalled();
  });

  it('keys the dismissal by the person: another person is still prompted', async () => {
    dismissPrompt('sub-ana');
    consents({ Enabled: true, Consents: [] });
    renderLanding();
    expect(await screen.findByText('my consents page')).toBeTruthy();
  });

  it('lands on the home page when the consents cannot be read', async () => {
    graphql.mockRejectedValue(new Error('network'));
    renderLanding();
    expect(await screen.findByText('home page')).toBeTruthy();
  });
});
