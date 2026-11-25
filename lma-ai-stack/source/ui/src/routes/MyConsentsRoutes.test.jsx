/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * My consents for an Admin or a User is an in-app page in the app's standard
 * layout: the side navigation (with My consents marked, opening in the same
 * tab), breadcrumbs, notifications and the help panel.
 */
import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../components/call-analytics-top-navigation', () => ({ default: () => null }));
vi.mock('../contexts/app', () => ({
  default: () => ({ navigationOpen: true, setNavigationOpen: () => {} }),
}));
vi.mock('../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../hooks/use-user-groups', () => ({ default: () => ({ isAdmin: false, isLoading: false }) }));
vi.mock('../components/my-consents/MyConsentsPage', () => ({ default: () => <div>member: my consents</div> }));

/* eslint-disable import/first */
import MyConsentsRoutes from './MyConsentsRoutes';
import { generateNavigationItems } from '../components/common/navigation-items';
/* eslint-enable import/first */

afterEach(cleanup);

describe('My consents for an Admin or a User', () => {
  it('renders in the standard layout with navigation, breadcrumbs and the help panel', () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/consents']}>
        <MyConsentsRoutes />
      </MemoryRouter>,
    );
    expect(screen.getByText('member: my consents')).toBeTruthy();
    const nav = screen.getByRole('navigation', { name: 'Side navigation' });
    const link = within(nav).getByRole('link', { name: 'My consents' });
    expect(link.getAttribute('target')).toBeNull();
    expect(link.getAttribute('aria-current')).toBe('page');
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumbs' });
    expect(within(crumbs).getAllByText('My consents').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: 'Open help panel' }));
    expect(screen.getByText('What a consent can cover')).toBeTruthy();
    // Every link to My consents opens in the same tab. (The meeting sources
    // above it deliberately open their own tab.)
    const toConsents = [...container.querySelectorAll('a[href^="#/consents"]')];
    expect(toConsents.length).toBeGreaterThan(0);
    expect(toConsents.filter((a) => a.getAttribute('target') === '_blank')).toEqual([]);
  });

  it('offers My consents in the navigation as an in-app link, for both roles', () => {
    [false, true].forEach((isAdmin) => {
      const entry = generateNavigationItems({}, isAdmin).find((item) => item.text === 'My consents');
      expect(entry.href).toBe('#/consents');
      expect(entry.external).toBeFalsy();
    });
  });
});
