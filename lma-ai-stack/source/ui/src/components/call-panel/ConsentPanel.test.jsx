/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Consent panel shows what the backend stored and offers each viewer only
 * what the backend would let them do: anyone who can change the meeting may
 * record an objection; the owner and Admins may run an evidence check; only the
 * owner may attest.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import ConsentPanel from './ConsentPanel';

/* eslint-disable react/prop-types */

const CONSENT = {
  Mode: 'opt_in',
  Rule: 'owner_attests',
  Status: 'pending',
  CaptureAllowed: false,
  NoticeText: 'Ana would like to record this meeting.',
  Participants: ['Bo Chen'],
  Events: [],
  Evidence: [
    {
      EvidenceId: 'e1',
      Kind: 'screenshot',
      CapturedAt: '2026-11-02T10:02:30.000Z',
      SignedUrl: 'https://example.com/e1.jpg',
    },
  ],
  StatusHistory: [{ Status: 'pending', CaptureAllowed: false, At: '2026-11-02T10:00:00.000Z', Reason: 'waiting' }],
};

const stateWith = (overrides = {}) => ({
  consent: CONSENT,
  error: null,
  recordObjection: vi.fn().mockResolvedValue(null),
  attest: vi.fn().mockResolvedValue(null),
  verifyEvidence: vi.fn().mockResolvedValue(null),
  ...overrides,
});

describe('ConsentPanel', () => {
  it('says so when the record could not be read', () => {
    render(<ConsentPanel state={stateWith({ consent: null, error: 'Network error' })} />);
    expect(screen.getByText('The consent record could not be read: Network error')).toBeInTheDocument();
  });

  it('offers attestation only where it grants the meeting', () => {
    const eachParticipant = { ...CONSENT, Rule: 'each_participant' };
    render(<ConsentPanel state={stateWith({ consent: eachParticipant })} canEdit isOwner />);
    expect(screen.queryByText('Attest that everyone agreed')).not.toBeInTheDocument();
  });

  it('shows nothing for a meeting whose consent was never started', () => {
    const { container } = render(<ConsentPanel state={stateWith({ consent: null })} canEdit isOwner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the mode, the status, the gate, the notice and the evidence image', () => {
    render(<ConsentPanel state={stateWith()} />);
    expect(screen.getByText('Opt-in (consent needed first)')).toBeInTheDocument();
    expect(screen.getByText('The meeting owner attests that everyone agreed')).toBeInTheDocument();
    expect(screen.getByText('Pending')).toBeInTheDocument();
    expect(screen.getByText('Withheld')).toBeInTheDocument();
    expect(screen.getByText('Ana would like to record this meeting.')).toBeInTheDocument();
    expect(screen.getByRole('img')).toHaveAttribute('src', 'https://example.com/e1.jpg');
    expect(screen.getByText('Not checked')).toBeInTheDocument();
  });

  it('offers a viewer who may not change the meeting no actions', () => {
    render(<ConsentPanel state={stateWith()} />);
    expect(screen.queryByText('Record an objection')).not.toBeInTheDocument();
    expect(screen.queryByText('Attest that everyone agreed')).not.toBeInTheDocument();
    expect(screen.queryByText('Check with model')).not.toBeInTheDocument();
  });

  it('lets somebody the meeting is shared with record an objection, but not attest or check', async () => {
    const state = stateWith();
    render(<ConsentPanel state={state} canEdit />);
    expect(screen.queryByText('Attest that everyone agreed')).not.toBeInTheDocument();
    expect(screen.queryByText('Check with model')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Record an objection'));
    fireEvent.change(screen.getByLabelText('Participant name'), { target: { value: ' Bo Chen ' } });
    fireEvent.click(screen.getAllByText('Record objection').find((el) => el.closest('button')));
    await waitFor(() => expect(state.recordObjection).toHaveBeenCalledWith('Bo Chen', ''));
  });

  it('lets the owner attest', async () => {
    const state = stateWith();
    render(<ConsentPanel state={state} canEdit isOwner />);
    fireEvent.click(screen.getByText('Attest that everyone agreed'));
    fireEvent.click(screen.getByText('Attest'));
    await waitFor(() => expect(state.attest).toHaveBeenCalledWith('verbal', ''));
  });

  it('shows a refused action in the dialog', async () => {
    const state = stateWith({ attest: vi.fn().mockResolvedValue('Unauthorized') });
    render(<ConsentPanel state={state} canEdit isOwner />);
    fireEvent.click(screen.getByText('Attest that everyone agreed'));
    fireEvent.click(screen.getByText('Attest'));
    expect(await screen.findByText('Unauthorized')).toBeInTheDocument();
  });

  it('runs an evidence check and shows its verdict once stored', async () => {
    const state = stateWith();
    const { rerender } = render(<ConsentPanel state={state} canEdit isAdmin />);
    fireEvent.click(screen.getByText('Check with model'));
    await waitFor(() => expect(state.verifyEvidence).toHaveBeenCalledWith('e1'));
    const checked = {
      ...CONSENT,
      Evidence: [
        {
          ...CONSENT.Evidence[0],
          Verification: {
            Verdict: 'objection_found',
            Confidence: 0.9,
            ModelId: 'amazon.nova-lite-v1:0',
            Summary: 'Bo objects.',
          },
        },
      ],
    };
    rerender(<ConsentPanel state={{ ...state, consent: checked }} canEdit isAdmin />);
    expect(screen.getByText('Objection found')).toBeInTheDocument();
    expect(screen.getByText('Bo objects.')).toBeInTheDocument();
    expect(screen.getByText('Check again')).toBeInTheDocument();
  });

  it('says when an image was deleted after its check', () => {
    const deleted = { ...CONSENT, Evidence: [{ EvidenceId: 'e1', ImageDeletedAt: '2026-11-02T10:03:00.000Z' }] };
    render(<ConsentPanel state={stateWith({ consent: deleted })} canEdit isOwner />);
    expect(screen.getByText('Image deleted after its check')).toBeInTheDocument();
    expect(screen.queryByText('Check with model')).not.toBeInTheDocument();
  });

  it('says when an image has expired', () => {
    render(<ConsentPanel state={stateWith()} canEdit />);
    fireEvent.error(screen.getByRole('img'));
    expect(screen.getByText('Image no longer available')).toBeInTheDocument();
  });
});
