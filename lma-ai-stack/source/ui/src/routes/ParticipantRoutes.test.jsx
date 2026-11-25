/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

vi.mock('../components/call-analytics-top-navigation', () => ({ default: () => null }));
vi.mock('../contexts/app', () => ({
  default: () => ({ navigationOpen: false, setNavigationOpen: () => {} }),
}));
// Every member page, the settings read and the Virtual Participant alerts are
// stubbed: a Participant must reach none of them.
vi.mock('./CallsRoutes', () => ({ default: () => <div>member: meetings</div> }));
vi.mock('../hooks/use-parameter-store', () => ({ default: () => ({}) }));
vi.mock('../components/virtual-participant-layout/ManualActionAlerts', () => ({ default: () => null }));
// The Participant's own pages, which read the API.
vi.mock('../components/participant-meetings/ParticipantMeetingList', () => ({
  default: () => <div>participant: shared meetings</div>,
}));
vi.mock('../components/participant-meetings/ParticipantMeetingPage', () => ({
  // The meeting page's Info link opens the layout's help panel.
  default: ({ callId, setToolsOpen }) => (
    <div>
      participant: meeting {callId}
      <button type="button" onClick={() => setToolsOpen(true)}>
        meeting info
      </button>
    </div>
  ),
}));
vi.mock('../components/voice-enrollment/VoiceEnrollmentPage', () => ({
  default: () => <div>participant: voice enrollment</div>,
}));
vi.mock('../components/my-consents/MyConsentsPage', () => ({
  default: ({ offerPrompt }) => <div>{`participant: my consents${offerPrompt ? ' (with the prompt)' : ''}`}</div>,
}));
// Where signing in with no link lands is decided by reading the person's
// consents (SignInLanding.test.jsx); here it only has to be reached.
vi.mock('../components/my-consents/SignInLanding', () => ({ default: () => <div>sign-in landing</div> }));

/* eslint-disable import/first */
import ParticipantRoutes, { afterSignIn } from './ParticipantRoutes';
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

const HOME_TEXT = /The meetings their owners have shared with you are under/;

describe('a Participant', () => {
  it('opens their home page', () => {
    renderAt('/home');
    expect(screen.getByText(HOME_TEXT)).toBeTruthy();
  });

  it('is taken to their home page from the root, and to the sign-in landing from sign-in', () => {
    renderAt('/');
    expect(screen.getByText(HOME_TEXT)).toBeTruthy();
    cleanup();
    renderAt('/login');
    expect(screen.getByText('sign-in landing')).toBeTruthy();
  });

  it.each(routesFor(false).flatMap((route) => [route, `${route}/some-id`]))('is denied %s', (route) => {
    renderAt(route);
    expect(screen.getByText('Access denied')).toBeTruthy();
    expect(screen.queryByText(HOME_TEXT)).toBeNull();
  });

  it('opens the meetings shared with them, a meeting by its id, and their voice enrollment', () => {
    renderAt('/shared-meetings');
    expect(screen.getByText('participant: shared meetings')).toBeTruthy();
    cleanup();
    renderAt(`/shared-meetings/${encodeURIComponent('Weekly sync - 2026+11 #1')}`);
    expect(screen.getByText('participant: meeting Weekly sync - 2026+11 #1')).toBeTruthy();
    cleanup();
    renderAt('/voice-enrollment');
    expect(screen.getByText('participant: voice enrollment')).toBeTruthy();
  });

  it("opens My consents, and a meeting's consent link after signing in", () => {
    renderAt('/consents');
    expect(screen.getByText('participant: my consents (with the prompt)')).toBeTruthy();
    cleanup();
    renderAt('/login', <ParticipantRoutes redirectParam="/consents?meeting=Weekly%20sync" />);
    expect(screen.getByText('participant: my consents (with the prompt)')).toBeTruthy();
    expect(screen.queryByText('sign-in landing')).toBeNull();
  });

  it('shows My consents in the standard layout: navigation, breadcrumbs and the help panel', async () => {
    const app = await import('../contexts/app');
    const spy = vi.spyOn(app, 'default').mockReturnValue({ navigationOpen: true, setNavigationOpen: () => {} });
    const { container } = renderAt('/consents');
    const nav = screen.getByRole('navigation', { name: 'Side navigation' });
    const link = within(nav).getByRole('link', { name: 'My consents' });
    expect(link.getAttribute('target')).toBeNull();
    expect(within(nav).getByRole('link', { name: 'Shared meetings' })).toBeTruthy();
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumbs' });
    expect(within(crumbs).getAllByText('My consents').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Open help panel' })).toBeTruthy();
    // The in-app links of the Participant's navigation open in the same tab.
    const inApp = [...container.querySelectorAll('a[href^="#/"]')];
    expect(inApp.length).toBeGreaterThan(0);
    expect(inApp.filter((a) => a.getAttribute('target') === '_blank')).toEqual([]);
    spy.mockRestore();
  });

  it('follows a sign-in redirect only to a page of its own', () => {
    expect(afterSignIn('/consents?vp=abc')).toBe('/consents?vp=abc');
    expect(afterSignIn('/shared-meetings')).toBe('/shared-meetings');
    ['', '/calls', '//evil.example/x', 'https://evil.example', '/login', '/configuration/settings'].forEach(
      (redirect) => expect(afterSignIn(redirect)).toBe('/home'),
    );
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

describe("a Participant page's frame", () => {
  const PAGES = {
    '/home': ['Home'],
    '/shared-meetings': ['Home', 'Shared meetings'],
    [`/shared-meetings/${encodeURIComponent('Weekly sync-2026-11-03-14-05-09-123')}`]: [
      'Home',
      'Shared meetings',
      'Weekly sync',
    ],
    // My consents has its own breadcrumbs, shared with every role.
    '/consents': ['Meeting Assistant', 'My consents'],
    '/voice-enrollment': ['Home', 'Voice enrollment'],
  };

  it.each(Object.keys(PAGES))(
    'is the app layout on %s: side navigation, breadcrumbs, notifications and the help panel',
    (route) => {
      const { container } = renderAt(route);
      const layout = createWrapper(container).findAppLayout();
      expect(layout).toBeTruthy();
      expect(layout.findNavigation().findSideNavigation()).toBeTruthy();
      expect(
        layout
          .findBreadcrumbs()
          .findBreadcrumbGroup()
          .findBreadcrumbLinks()
          .map((link) => link.getElement().textContent),
      ).toEqual(PAGES[route]);
      expect(layout.findNotifications()).toBeTruthy();
      expect(layout.findToolsToggle()).toBeTruthy();
    },
  );

  it("opens the page's help from the (i) button", () => {
    const { container } = renderAt('/shared-meetings');
    const layout = createWrapper(container).findAppLayout();
    expect(layout.findOpenToolsPanel()).toBeNull();
    layout.findToolsToggle().click();
    expect(layout.findOpenToolsPanel()).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Shared meetings', level: 2 })).toBeTruthy();
  });

  it("opens a meeting's help from the meeting page's Info link", () => {
    const { container } = renderAt(`/shared-meetings/${encodeURIComponent('Weekly sync')}`);
    const layout = createWrapper(container).findAppLayout();
    expect(layout.findOpenToolsPanel()).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'meeting info' }));
    expect(layout.findOpenToolsPanel()).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Shared meeting', level: 2 })).toBeTruthy();
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
