/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

import { ADMIN, PARTICIPANT, ROLES, ROUTE_ROLES, USER, effectiveRole, groupsNameARole, roleMayOpen } from './roles';
import { generateNavigationItems } from './navigation-items';
import { participantNavigationItems } from '../participant-home/ParticipantHome';

// The role matrix: lma-ai-stack/source/appsync/role-matrix.json.
const MATRIX = JSON.parse(readFileSync(path.join(__dirname, '../../../../appsync/role-matrix.json'), 'utf-8'));

const internalLinks = (items) =>
  items
    .flatMap((entry) => (entry.type === 'section' ? entry.items : [entry]))
    .filter((item) => item.type === 'link' && item.href.startsWith('#/'))
    .map((item) => item.href.slice(1).split('?')[0]);

describe('roles', () => {
  it('names the three roles of the role matrix', () => {
    expect(ROLES).toEqual(Object.keys(MATRIX.roles));
  });

  it.each(MATRIX.effectiveRole.vectors)(
    'reads groups $groups as $role, as the role matrix does',
    ({ groups, role }) => {
      expect(effectiveRole(groups)).toBe(role);
    },
  );

  it('reads a bare group string as that one group', () => {
    expect(effectiveRole('Participant')).toBe(PARTICIPANT);
    expect(effectiveRole('Admin')).toBe(ADMIN);
  });

  it('knows a token from the trigger, which always names a role', () => {
    expect(groupsNameARole(['User'])).toBe(true);
    expect(groupsNameARole(['Sales', 'Participant'])).toBe(true);
    expect(groupsNameARole([])).toBe(false);
    expect(groupsNameARole(['Sales'])).toBe(false);
  });

  it('gives every page the roles the role matrix lists for its route', () => {
    const fromMatrix = Object.fromEntries(
      MATRIX.capabilities.flatMap((capability) => capability.uiRoutes.map((route) => [route, capability.roles])),
    );
    const sorted = (table) =>
      Object.fromEntries(Object.entries(table).map(([route, roles]) => [route, [...roles].sort()]));
    expect(sorted(ROUTE_ROLES)).toEqual(sorted(fromMatrix));
  });

  it('decides a page by the most specific route that contains it, and refuses a path under none', () => {
    expect(roleMayOpen(USER, '/calls/abc-123')).toBe(true);
    expect(roleMayOpen(PARTICIPANT, '/calls/abc-123')).toBe(false);
    expect(roleMayOpen(PARTICIPANT, '/calls/query')).toBe(false);
    expect(roleMayOpen(PARTICIPANT, '/home')).toBe(true);
    expect(roleMayOpen(USER, '/home')).toBe(false);
    expect(roleMayOpen(USER, '/configuration/settings')).toBe(false);
    expect(roleMayOpen(ADMIN, '/configuration/settings')).toBe(true);
    expect(roleMayOpen(USER, '/configuration/skills')).toBe(true);
    expect(roleMayOpen(ADMIN, '/no-such-page')).toBe(false);
    expect(roleMayOpen(PARTICIPANT, '/homepage')).toBe(false);
  });
});

describe('navigation by role', () => {
  const settings = { AsrEngineAvailable: 'true', VoiceEnrollmentAvailable: 'true' };

  it.each([
    [ADMIN, true],
    [USER, false],
  ])('links a %s only to pages that role may open', (role, isAdmin) => {
    const refused = internalLinks(generateNavigationItems(settings, isAdmin)).filter(
      (route) => !roleMayOpen(role, route),
    );
    expect(refused).toEqual([]);
  });

  it('links a Participant to their home page only', () => {
    expect(internalLinks(participantNavigationItems())).toEqual(['/home']);
  });

  it('links a User to no Admin-only page', () => {
    const adminOnly = Object.entries(ROUTE_ROLES)
      .filter(([, roles]) => !roles.includes(USER))
      .map(([route]) => route);
    expect(internalLinks(generateNavigationItems(settings, false)).filter((r) => adminOnly.includes(r))).toEqual([]);
  });
});
