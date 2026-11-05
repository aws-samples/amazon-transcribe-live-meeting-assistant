/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';

import LaunchSettings from './LaunchSettings';

const SCHEDULED_ZOOM = {
  id: 'vp-1',
  meetingName: 'Weekly sync',
  meetingPlatform: 'ZOOM',
  meetingId: '1234567890',
  hasMeetingPassword: true,
  useStoredZoomAccount: true,
  isScheduled: true,
  meetingTime: Date.UTC(2026, 10, 10, 19, 0, 0) / 1000,
  scheduledTimeZone: 'America/New_York',
  agenda: '1. Introductions\n2. Budget',
  agendaSource: 'scheduled',
  enableVideoRecording: false,
  consentMode: 'notify',
};

const expand = () => fireEvent.click(screen.getByText('Launch settings'));

describe('LaunchSettings', () => {
  it('lists every value the form asked for, grouped', () => {
    render(<LaunchSettings vp={SCHEDULED_ZOOM} />);
    expand();

    ['Meeting', 'Capture', 'Consent'].forEach((group) => expect(screen.getByText(group)).toBeTruthy());
    expect(screen.getByText('Weekly sync')).toBeTruthy();
    expect(screen.getByText('Zoom')).toBeTruthy();
    expect(screen.getByText('1234567890')).toBeTruthy();
    expect(screen.getByText("The launching user's stored Zoom account")).toBeTruthy();
    expect(screen.getByText(/\(America\/New_York\)$/)).toBeTruthy();
    expect(screen.getByText('Off')).toBeTruthy();
    expect(screen.getByText(/Notify: post a notice/)).toBeTruthy();
  });

  it('shows that a password was set, never its value', () => {
    const { container } = render(<LaunchSettings vp={{ ...SCHEDULED_ZOOM, meetingPassword: 'hunter2' }} />);
    expand();

    expect(screen.getByText('Set (not shown)')).toBeTruthy();
    expect(container.textContent).not.toContain('hunter2');
  });

  it('opens the agenda as a numbered list', () => {
    render(<LaunchSettings vp={SCHEDULED_ZOOM} />);
    expand();
    fireEvent.click(screen.getByText('2 items'));

    expect(screen.getByText('Introductions')).toBeTruthy();
    expect(screen.getByText('Budget')).toBeTruthy();
    expect(screen.getByText('From scheduling')).toBeTruthy();
  });

  it('reads absent choices on an immediate launch as defaults', () => {
    render(
      <LaunchSettings
        vp={{ id: 'vp-2', meetingName: 'Now', meetingPlatform: 'TEAMS', meetingId: '42', hasMeetingPassword: false }}
      />,
    );
    expand();

    expect(screen.getByText('Start now')).toBeTruthy();
    expect(screen.getAllByText('Not set (deployment default)')).toHaveLength(2);
    expect(screen.queryByText('Zoom account')).toBeNull();
    // The meeting password and the agenda.
    expect(screen.getAllByText('Not set')).toHaveLength(2);
  });
});
