/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The portal's consent flow.
 *
 * These are not ordinary UI tests. Each one pins a claim the stored consent
 * record attests to -- that the person read the terms, that they took an
 * explicit affirmative action, and that no audio was captured before they did.
 * If the page stops behaving this way, the record stops meaning what it says.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

/* eslint-disable react/prop-types */

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
import VoiceEnrollmentPage from './VoiceEnrollmentPage';

const TERMS = [
  'You are being asked to let this deployment recognise your voice.',
  'What is stored. A voiceprint: a set of numbers derived from your voice.',
  'Withdrawing. You can withdraw at any time, from this page.',
].join('\n\n');

const getUserMedia = vi.fn();

const state = (overrides = {}) => ({
  Success: true,
  Status: 'none',
  DisplayName: '',
  ConsentExpiresAt: '',
  NeedsReconsent: false,
  TermsVersion: '1',
  Terms: TERMS,
  ...overrides,
});

const respond = (payload) => ({ data: payload });

beforeEach(() => {
  vi.clearAllMocks();
  getUserMedia.mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] });
  Object.defineProperty(window.navigator, 'mediaDevices', {
    value: { getUserMedia },
    configurable: true,
  });
});

const renderWith = (initial, ...rest) => {
  graphql.mockResolvedValueOnce(respond({ voiceEnrollmentTerms: initial }));
  rest.forEach((r) => graphql.mockResolvedValueOnce(r));
  render(<VoiceEnrollmentPage />);
};

describe('before anybody has agreed', () => {
  it('shows the terms in full, as given', async () => {
    // Rendered as supplied rather than summarised: a summary is not what the
    // stored record attests to, and the hash is of the text the backend sent.
    renderWith(state());
    await waitFor(() => expect(screen.getByText(/recognise your voice/)).toBeTruthy());
    expect(screen.getByText(/A voiceprint: a set of numbers/)).toBeTruthy();
    expect(screen.getByText(/withdraw at any time/)).toBeTruthy();
  });

  it('starts with the agreement box unticked', async () => {
    // No pre-ticked box, and no consent implied by arriving on the page.
    renderWith(state());
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeTruthy());
    expect(screen.getByRole('checkbox').checked).toBe(false);
  });

  it('will not accept an agreement until the box is ticked', async () => {
    renderWith(state());
    await waitFor(() => expect(screen.getByText('I agree')).toBeTruthy());
    expect(screen.getByText('I agree').closest('button').disabled).toBe(true);
  });

  it('needs two separate acts: tick the box, then press the button', async () => {
    renderWith(state(), respond({ grantVoiceConsent: { Success: true, Status: 'pending' } }));
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeTruthy());

    fireEvent.click(screen.getByRole('checkbox'));
    // Ticking alone must not record anything.
    expect(graphql).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('I agree').closest('button'));
    await waitFor(() => expect(graphql).toHaveBeenCalledTimes(3));
  });

  it('offers no way to record audio', async () => {
    // The ordering is the point: consent is on record before any audio exists.
    renderWith(state());
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeTruthy());
    expect(screen.queryByText('Record a line')).toBeNull();
  });

  it('never touches the microphone', async () => {
    renderWith(state());
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeTruthy());
    fireEvent.click(screen.getByRole('checkbox'));
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it('offers declining as an ordinary outcome', async () => {
    renderWith(state());
    await waitFor(() => expect(screen.getByText('No thanks')).toBeTruthy());
  });
});

describe('after agreeing, before recording', () => {
  it('offers the recording step and a phonetically varied script', async () => {
    renderWith(state({ Status: 'pending' }));
    await waitFor(() => expect(screen.getByText('Record a line')).toBeTruthy());
    expect(screen.getByText(/quick brown fox/)).toBeTruthy();
    expect(screen.getByText(/Seventeen, forty-two/)).toBeTruthy();
  });

  it('will not enrol without enough speech', async () => {
    renderWith(state({ Status: 'pending' }));
    await waitFor(() => expect(screen.getByText('Enrol my voice')).toBeTruthy());
    expect(screen.getByText('Enrol my voice').closest('button').disabled).toBe(true);
  });

  it('says the recordings are not kept', async () => {
    // The terms promise it, so the page that takes them should repeat it.
    renderWith(state({ Status: 'pending' }));
    await waitFor(() => expect(screen.getByText(/are then discarded/)).toBeTruthy());
  });

  it('asks for the name to label transcripts with', async () => {
    renderWith(state({ Status: 'pending' }));
    await waitFor(() => expect(screen.getByPlaceholderText('e.g. Sarah Lee')).toBeTruthy());
  });
});

describe('once enrolled', () => {
  const enrolled = state({ Status: 'active', DisplayName: 'Sarah Lee', ConsentExpiresAt: '2027-10-18T00:00:00Z' });

  it('says who meetings will call them, and until when', async () => {
    renderWith(enrolled);
    await waitFor(() => expect(screen.getByText('Sarah Lee')).toBeTruthy());
    expect(screen.getByText(/2027-10-18/)).toBeTruthy();
  });

  it('offers withdrawal as plainly as it offered enrolling', async () => {
    // Withdrawal needs nobody's permission, so it is a button on this page and
    // not a request to somebody else.
    renderWith(enrolled);
    await waitFor(() => expect(screen.getByText('Delete my voiceprint')).toBeTruthy());
  });

  it('withdraws without asking anything further', async () => {
    renderWith(
      enrolled,
      respond({ revokeVoiceConsent: { Success: true, Status: 'revoked' } }),
      respond({ voiceEnrollmentTerms: state({ Status: 'revoked' }) }),
    );
    await waitFor(() => expect(screen.getByText('Delete my voiceprint')).toBeTruthy());
    fireEvent.click(screen.getByText('Delete my voiceprint').closest('button'));
    await waitFor(() => expect(screen.getByText(/has been deleted/)).toBeTruthy());
  });

  it('does not offer the terms again while consent stands', async () => {
    renderWith(enrolled);
    await waitFor(() => expect(screen.getByText('Sarah Lee')).toBeTruthy());
    expect(screen.queryByRole('checkbox')).toBeNull();
  });
});

describe('when the terms have changed', () => {
  const stale = state({ Status: 'active', DisplayName: 'Sarah Lee', NeedsReconsent: true });

  it('says so, and says the voiceprint is not being used', async () => {
    renderWith(stale);
    await waitFor(() => expect(screen.getByText('The terms have changed')).toBeTruthy());
    expect(screen.getByText(/not being used/)).toBeTruthy();
  });

  it('asks for a fresh agreement rather than assuming the old one', async () => {
    renderWith(stale);
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeTruthy());
    expect(screen.getByRole('checkbox').checked).toBe(false);
  });
});

describe('when something fails', () => {
  it('says so rather than failing silently', async () => {
    graphql.mockRejectedValueOnce(new Error('nope'));
    render(<VoiceEnrollmentPage />);
    await waitFor(() => expect(screen.getByText(/Could not load this page/)).toBeTruthy());
  });

  it('reports a refused consent without claiming success', async () => {
    renderWith(state(), respond({ grantVoiceConsent: { Success: false, Message: 'not permitted' } }));
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeTruthy());
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByText('I agree').closest('button'));
    await waitFor(() => expect(screen.getByText('not permitted')).toBeTruthy());
  });
});
