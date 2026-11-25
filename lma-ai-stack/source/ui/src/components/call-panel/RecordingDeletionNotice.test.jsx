/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The notice above the transcript says when the Retention settings delete the
 * meeting's recordings, or when they did, and nothing for a meeting they leave
 * alone or that had nothing to delete.
 */
import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import RecordingDeletionNotice from './RecordingDeletionNotice';

describe('RecordingDeletionNotice', () => {
  it('shows nothing for a meeting whose recordings are kept', () => {
    const { container } = render(<RecordingDeletionNotice hasRecordings />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says when the recordings are due to be deleted, while there is one to download', () => {
    render(<RecordingDeletionNotice scheduledFor="2026-11-02T10:30:00.000Z" hasRecordings />);
    expect(screen.getByText('Recordings to be deleted')).toBeInTheDocument();
    expect(screen.getByText(/Download them before then/)).toBeInTheDocument();
  });

  it('says nothing of a pending deletion for a meeting with no recording', () => {
    const { container } = render(<RecordingDeletionNotice scheduledFor="2026-11-02T10:30:00.000Z" />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says the recordings were deleted, which outranks a pending time', () => {
    render(
      <RecordingDeletionNotice
        deletedAt="2026-11-02T10:30:00.000Z"
        scheduledFor="2026-11-02T10:40:00.000Z"
        filesDeleted={2}
      />,
    );
    expect(screen.getByText('Recordings deleted')).toBeInTheDocument();
    expect(screen.getByText(/can no longer be played or downloaded/)).toBeInTheDocument();
    expect(screen.getByText(/shared-screen slides are kept/)).toBeInTheDocument();
    expect(screen.queryByText('Recordings to be deleted')).not.toBeInTheDocument();
  });

  it('says nothing for a meeting that was never recorded', () => {
    const { container } = render(<RecordingDeletionNotice deletedAt="2026-11-02T10:30:00.000Z" filesDeleted={0} />);
    expect(container).toBeEmptyDOMElement();
  });
});
