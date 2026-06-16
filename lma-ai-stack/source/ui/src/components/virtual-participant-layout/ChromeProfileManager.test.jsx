/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import ChromeProfileManager from './ChromeProfileManager';

// Mock the Amplify client before importing the component (generateClient() runs
// at module load). graphqlMock is the single fn both the query and mutation go
// through, so we can assert on the variables it receives. vi.hoisted keeps the
// mock fn defined before the hoisted vi.mock factory references it.
const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));

const statusResponse = (present = false) => ({
  data: { getMyChromeProfileStatus: { present, sizeBytes: null, lastModified: null } },
});

describe('ChromeProfileManager', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  it('requests status scoped to the selected platform', async () => {
    graphqlMock.mockResolvedValue(statusResponse(false));

    render(<ChromeProfileManager platform="WEBEX" />);

    await waitFor(() => expect(graphqlMock).toHaveBeenCalled());
    expect(graphqlMock).toHaveBeenCalledWith(expect.objectContaining({ variables: { platform: 'WEBEX' } }));
  });

  it('re-fetches status when the platform prop changes', async () => {
    graphqlMock.mockResolvedValue(statusResponse(false));

    const { rerender } = render(<ChromeProfileManager platform="ZOOM" />);
    await waitFor(() =>
      expect(graphqlMock).toHaveBeenCalledWith(expect.objectContaining({ variables: { platform: 'ZOOM' } })),
    );

    rerender(<ChromeProfileManager platform="TEAMS" />);
    await waitFor(() =>
      expect(graphqlMock).toHaveBeenCalledWith(expect.objectContaining({ variables: { platform: 'TEAMS' } })),
    );
  });

  it('passes the platform when removing the profile', async () => {
    // First call: status (present so the Remove button renders). Subsequent
    // calls resolve generically.
    graphqlMock.mockResolvedValue(statusResponse(true));

    render(<ChromeProfileManager platform="CHIME" />);

    // Open the confirm modal, then confirm removal.
    const removeButtons = await screen.findAllByText('Remove Profile');
    fireEvent.click(removeButtons[0]);
    const confirmButtons = await screen.findAllByText('Remove Profile');
    // The modal's confirm button is the last "Remove Profile" rendered.
    fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() =>
      expect(graphqlMock).toHaveBeenCalledWith(
        expect.objectContaining({
          query: expect.stringContaining('DeleteMyChromeProfile'),
          variables: { platform: 'CHIME' },
        }),
      ),
    );
  });
});
