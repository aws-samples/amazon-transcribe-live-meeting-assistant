/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The invitation page.
 *
 * The claim worth pinning is the asymmetry the whole design rests on: an admin
 * can ask, and only the person asked can agree. So these check that the page
 * sends invitations and says so plainly, that it never claims to have enrolled
 * anybody, and that one address which cannot be invited does not cost the
 * others theirs.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    /* eslint-disable class-methods-use-this */
    debug() {}

    info() {}

    warn() {}

    error() {}
    /* eslint-enable class-methods-use-this */
  },
}));

// eslint-disable-next-line import/first
import VoiceEnrollmentAdminPage, { parseInviteList } from './VoiceEnrollmentAdminPage';

const typeAddresses = (value) => {
  const textarea = screen.getByPlaceholderText(/sarah@example.com/);
  fireEvent.change(textarea, { target: { value } });
  return textarea;
};

const sendButton = () => screen.getByRole('button', { name: /Send/ });

describe('parseInviteList', () => {
  it('accepts one address per line', () => {
    expect(parseInviteList('a@example.com\nb@example.com')).toEqual(['a@example.com', 'b@example.com']);
  });

  it('accepts commas and semicolons, as a pasted recipient line uses', () => {
    expect(parseInviteList('a@example.com, b@example.com; c@example.com')).toEqual([
      'a@example.com',
      'b@example.com',
      'c@example.com',
    ]);
  });

  it('reduces a display name to the address inside it', () => {
    expect(parseInviteList('Sarah Lee <sarah@example.com>')).toEqual(['sarah@example.com']);
  });

  it('does not invite the same person twice for one paste', () => {
    // Case-insensitively: a duplicate would create a second invitation row for
    // somebody who has already been asked.
    expect(parseInviteList('a@example.com\nA@Example.com')).toEqual(['a@example.com']);
  });

  it('ignores blank lines and stray separators', () => {
    expect(parseInviteList('\n\na@example.com,,\n ;\n')).toEqual(['a@example.com']);
  });

  it('is empty for empty input', () => {
    expect(parseInviteList('')).toEqual([]);
    expect(parseInviteList(undefined)).toEqual([]);
  });
});

describe('VoiceEnrollmentAdminPage', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('says what an invitation is not', () => {
    render(<VoiceEnrollmentAdminPage />);
    expect(screen.getByText(/it is not a consent/i)).toBeTruthy();
    expect(screen.getByText(/Only the person invited can agree/i)).toBeTruthy();
  });

  it('cannot be sent with nothing entered', () => {
    render(<VoiceEnrollmentAdminPage />);
    expect(sendButton().getAttribute('disabled')).not.toBeNull();
  });

  it('sends one invitation per address and reports each', async () => {
    graphql.mockResolvedValue({
      data: { inviteVoiceEnrollee: { Success: true, Message: 'invitation sent' } },
    });
    render(<VoiceEnrollmentAdminPage />);
    typeAddresses('a@example.com\nb@example.com');
    fireEvent.click(sendButton());

    await waitFor(() => expect(graphql).toHaveBeenCalledTimes(2));
    expect(graphql.mock.calls[0][0].variables).toEqual({ input: { Email: 'a@example.com' } });
    expect(graphql.mock.calls[1][0].variables).toEqual({ input: { Email: 'b@example.com' } });
    await waitFor(() => expect(screen.getByText(/2 of 2 sent/)).toBeTruthy());
  });

  it('keeps going when one address is refused', async () => {
    // One person who cannot be invited -- an address outside the deployment's
    // allowed domain, say -- must not cost everybody else their invitation.
    graphql
      .mockResolvedValueOnce({
        data: { inviteVoiceEnrollee: { Success: false, Message: 'only example.net addresses' } },
      })
      .mockResolvedValueOnce({
        data: { inviteVoiceEnrollee: { Success: true, Message: 'invitation sent' } },
      });
    render(<VoiceEnrollmentAdminPage />);
    typeAddresses('a@example.com\nb@example.net');
    fireEvent.click(sendButton());

    await waitFor(() => expect(screen.getByText(/1 of 2 sent/)).toBeTruthy());
    expect(screen.getByText(/only example.net addresses/)).toBeTruthy();
  });

  it('keeps going when one invitation throws', async () => {
    graphql.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({
      data: { inviteVoiceEnrollee: { Success: true, Message: 'invitation sent' } },
    });
    render(<VoiceEnrollmentAdminPage />);
    typeAddresses('a@example.com\nb@example.com');
    fireEvent.click(sendButton());

    await waitFor(() => expect(screen.getByText(/1 of 2 sent/)).toBeTruthy());
  });

  it('will not send something that is not an email address', async () => {
    render(<VoiceEnrollmentAdminPage />);
    typeAddresses('not-an-address');
    await waitFor(() => expect(sendButton().getAttribute('disabled')).not.toBeNull());
    expect(screen.getByText(/Not an email address/)).toBeTruthy();
    expect(graphql).not.toHaveBeenCalled();
  });

  it('clears the list only when every invitation was sent', async () => {
    graphql.mockResolvedValue({
      data: { inviteVoiceEnrollee: { Success: false, Message: 'not sent' } },
    });
    render(<VoiceEnrollmentAdminPage />);
    const textarea = typeAddresses('a@example.com');
    fireEvent.click(sendButton());

    await waitFor(() => expect(screen.getByText(/0 of 1 sent/)).toBeTruthy());
    // Still there, so the admin can see what failed and retry it.
    expect(textarea.value).toBe('a@example.com');
  });

  it('offers no control that enrols anybody', () => {
    render(<VoiceEnrollmentAdminPage />);
    const labels = screen.getAllByRole('button').map((button) => button.textContent || '');
    expect(labels.some((label) => /enrol|record|voiceprint/i.test(label))).toBe(false);
  });
});
