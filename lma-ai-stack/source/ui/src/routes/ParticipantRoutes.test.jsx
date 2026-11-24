/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../components/call-analytics-top-navigation', () => ({ default: () => null }));
vi.mock('../contexts/app', () => ({
  default: () => ({ navigationOpen: false, setNavigationOpen: () => {} }),
}));
// Every member page, the settings read and the Virtual Participant alerts are
// stubbed: a Participant must reach none of them.
vi.mock('./CallsRoutes', () => ({ default: () => <div>member: meetings</div> }));
vi.mock('../hooks/use-parameter-store', () => ({ default: () => ({}) }));
vi.mock('../components/virtual-participant-layout/ManualActionAlerts', () => ({ default: () => null }));

/* eslint-disable import/first */
import ParticipantRoutes from './ParticipantRoutes';
import AuthRoutes from './AuthRoutes';
/* eslint-enable import/first */

const MATRIX = JSON.parse(readFileSync(path.join(__dirname, '../../../appsync/role-matrix.json'), 'utf-8'));

const routesFor = (allowed) =>
  MATRIX.capabilities.flatMap((capability) =>
    capability.uiRoutes.filter(() => capability.roles.includes('Participant') === allowed),
  );

const renderAt = (pathname, element = <ParticipantRoutes />) =>
  render(<MemoryRouter initialEntries={[pathname]}>{element}</MemoryRouter>);

afterEach(cleanup);

describe('a Participant', () => {
  it('opens their home page', () => {
    renderAt('/home');
    expect(screen.getByText('Your meetings and consents will appear here.')).toBeTruthy();
  });

  it('is taken to their home page from the root and from sign-in', () => {
    ['/', '/login'].forEach((start) => {
      renderAt(start);
      expect(screen.getByText('Your meetings and consents will appear here.')).toBeTruthy();
      cleanup();
    });
  });

  it.each(routesFor(false).flatMap((route) => [route, `${route}/some-id`]))('is denied %s', (route) => {
    renderAt(route);
    expect(screen.getByText('Access denied')).toBeTruthy();
    expect(screen.queryByText('Your meetings and consents will appear here.')).toBeNull();
  });

  it('is denied a path no route names', () => {
    renderAt('/no-such-page');
    expect(screen.getByText('Access denied')).toBeTruthy();
  });

  it('opens every route the role matrix lists for the role', () => {
    routesFor(true).forEach((route) => {
      renderAt(route);
      expect(screen.queryByText('Access denied')).toBeNull();
      cleanup();
    });
  });
});

describe('the signed-in routes by role', () => {
  const sessionWith = (groups) => ({ tokens: { idToken: { payload: { 'cognito:groups': groups } } } });

  const renderAuthRoutes = async (groups) => {
    const app = await import('../contexts/app');
    vi.spyOn(app, 'default').mockReturnValue({
      currentSession: sessionWith(groups),
      currentCredentials: {},
      navigationOpen: false,
      setNavigationOpen: () => {},
    });
    renderAt('/calls', <AuthRoutes redirectParam="" />);
  };

  it.each([[['User']], [[]], [['Admin']], [['Admin', 'Participant']]])(
    'gives groups %j the member routes',
    async (groups) => {
      await renderAuthRoutes(groups);
      expect(screen.getByText('member: meetings')).toBeTruthy();
    },
  );

  it('gives a Participant the Participant routes, so no member page mounts', async () => {
    await renderAuthRoutes(['Participant']);
    expect(screen.queryByText('member: meetings')).toBeNull();
    expect(screen.getByText('Access denied')).toBeTruthy();
  });
});
