/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The notice above the transcript says when the Retention settings delete it,
 * or when they did, and nothing for a meeting they leave alone.
 */
import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import TranscriptDeletionNotice from './TranscriptDeletionNotice';

describe('TranscriptDeletionNotice', () => {
  it('shows nothing for a meeting whose transcript is kept', () => {
    const { container } = render(<TranscriptDeletionNotice />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says when the transcript is due to be deleted', () => {
    render(<TranscriptDeletionNotice scheduledFor="2026-11-02T10:30:00.000Z" />);
    expect(screen.getByText('Transcript to be deleted')).toBeInTheDocument();
    expect(screen.getByText(/Download it before then/)).toBeInTheDocument();
  });

  it('says the transcript was deleted, which outranks a pending time', () => {
    render(<TranscriptDeletionNotice deletedAt="2026-11-02T10:30:00.000Z" scheduledFor="2026-11-02T10:40:00.000Z" />);
    expect(screen.getByText('Transcript deleted')).toBeInTheDocument();
    expect(screen.queryByText('Transcript to be deleted')).not.toBeInTheDocument();
  });
});
