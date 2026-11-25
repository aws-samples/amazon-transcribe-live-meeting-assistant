/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it } from 'vitest';

import {
  MAILTO_LIMIT,
  appBase,
  consentInvitation,
  consentLink,
  consentLinkMeeting,
  consentScopeLabel,
  consentStatusLabel,
  dateAhead,
  localDay,
} from './advance-consent';

const BASE = 'https://d111111abcdef8.cloudfront.net/';
const PEOPLE = [
  { name: 'Ana Silva', email: 'ana@example.com' },
  { name: '', email: 'bo@example.org' },
];

describe('the consent link', () => {
  it('names the meeting by CallId, or by the Virtual Participant before the meeting exists', () => {
    expect(consentLink(BASE, { callId: 'Weekly sync - 2026/11/30 #1' })).toBe(
      'https://d111111abcdef8.cloudfront.net/#/consents?meeting=Weekly%20sync%20-%202026%2F11%2F30%20%231',
    );
    expect(consentLink('https://lma.example.com/app/#', { virtualParticipantId: 'vp-1' })).toBe(
      'https://lma.example.com/app/#/consents?vp=vp-1',
    );
  });

  it("starts with the web UI's own address, without a page name", () => {
    expect(appBase({ origin: 'https://lma.example.com', pathname: '/index.html' })).toBe('https://lma.example.com/');
    expect(appBase({ origin: 'https://lma.example.com', pathname: '/' })).toBe('https://lma.example.com/');
  });

  it('is read back from the page query', () => {
    expect(consentLinkMeeting('?meeting=Weekly%20sync%20%231')).toEqual({ CallId: 'Weekly sync #1' });
    expect(consentLinkMeeting('?vp=vp-1')).toEqual({ VirtualParticipantId: 'vp-1' });
    expect(consentLinkMeeting('')).toBeNull();
    expect(consentLinkMeeting('?other=1')).toBeNull();
  });
});

describe('the invitation to consent', () => {
  const link = consentLink(BASE, { callId: 'Weekly sync' });

  it('is one email to everyone on the list with one link', () => {
    const invitation = consentInvitation({
      meetingName: 'Weekly sync',
      organizer: 'owner@example.com',
      scheduledFor: '2026-11-30T15:00:00.000Z',
      participants: PEOPLE,
      link,
    });
    expect(invitation.to).toEqual(['ana@example.com', 'bo@example.org']);
    expect(invitation.subject).toBe('Consent to recording: Weekly sync');
    expect(invitation.body.split(link)).toHaveLength(2);
    expect(invitation.body).toContain('Mon, 30 Nov 2026 15:00:00 UTC');
    expect(invitation.body).toContain('withdraw it at any time on the same page');
    // True in every consent mode, including Off, where no notice is posted.
    expect(invitation.body).not.toMatch(/notice|object/);
    expect(invitation.body).toContain('owner@example.com');
    expect(invitation.mailto.startsWith('mailto:ana%40example.com,bo%40example.org?subject=')).toBe(true);
    expect(decodeURIComponent(invitation.mailto.split('&body=')[1])).toBe(invitation.body);
    expect(invitation.shortened).toBe(false);
    expect(invitation.text).toContain('To: ana@example.com, bo@example.org');
  });

  it('leaves the recipients out of a mail link that would be too long, and says so', () => {
    const many = Array.from({ length: 100 }, (_, i) => ({ email: `person${i}@example.com` }));
    const invitation = consentInvitation({ meetingName: 'All hands', participants: many, link });
    expect(invitation.shortened).toBe(true);
    expect(invitation.mailto.startsWith('mailto:?subject=')).toBe(true);
    expect(invitation.mailto.length).toBeLessThan(MAILTO_LIMIT);
    expect(invitation.text).toContain('person99@example.com');
  });
});

describe('a stored consent', () => {
  it('says what it covers', () => {
    expect(consentScopeLabel({ Scope: 'meeting', MeetingName: 'Weekly sync' })).toBe('This meeting: Weekly sync');
    expect(consentScopeLabel({ Scope: 'organizer', Organizer: 'owner@example.com' })).toBe(
      'Meetings organized by owner@example.com',
    );
    expect(consentScopeLabel({ Scope: 'organizer', Organizer: 'o', LabelKey: 'engagement', LabelValue: 'acme' })).toBe(
      'Meetings organized by o, labelled engagement: acme',
    );
    expect(consentScopeLabel({ Scope: 'deployment', Organizer: null })).toBe('All meetings on this deployment');
  });

  it('labels its status', () => {
    expect(consentStatusLabel({ Status: 'active' })).toBe('In force');
    expect(consentStatusLabel({ Status: 'expired' })).toBe('Expired');
    expect(consentStatusLabel({ Status: 'withdrawn', WithdrawnReason: 'replaced' })).toBe('Replaced');
    expect(consentStatusLabel({ Status: 'withdrawn', WithdrawnReason: 'withdrawn' })).toBe('Withdrawn');
  });

  it('reads a date picker day as the local day it shows', () => {
    expect(localDay(new Date(2026, 10, 25))).toBe('2026-11-25');
    expect(localDay(new Date(2026, 0, 1, 23, 59))).toBe('2026-01-01');
  });

  it('dates a period in UTC days', () => {
    expect(dateAhead(30, new Date('2026-11-25T23:30:00Z'))).toBe('2026-12-25');
  });
});
