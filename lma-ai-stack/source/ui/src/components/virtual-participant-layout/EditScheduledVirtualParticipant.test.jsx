/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import LaunchSettings from './LaunchSettings';
import EditScheduledVirtualParticipant from './EditScheduledVirtualParticipant';

// generateClient() runs at module load in the modal and in the Zoom and
// browser-profile managers; every GraphQL call goes through graphqlMock.
const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    error() {} // eslint-disable-line class-methods-use-this
  },
}));

const HOUR_AHEAD = Math.floor(Date.now() / 1000 / 60) * 60 + 3600;

const SCHEDULED = {
  id: 'vp-1',
  Owner: 'owner@example.com',
  status: 'SCHEDULED',
  isScheduled: true,
  meetingName: 'Weekly sync',
  meetingPlatform: 'TEAMS',
  meetingId: '42',
  hasMeetingPassword: true,
  meetingTime: HOUR_AHEAD,
  enableVideoRecording: true,
};

const renderEditor = (props = {}) => {
  const onSaved = vi.fn();
  const onDismiss = vi.fn();
  render(
    <EditScheduledVirtualParticipant
      vp={SCHEDULED}
      visible
      onDismiss={onDismiss}
      onSaved={onSaved}
      isOwner
      {...props} // eslint-disable-line react/jsx-props-no-spreading
    />,
  );
  return { onSaved, onDismiss };
};

const typeInto = (label, value) => {
  const input = screen.getByPlaceholderText(label);
  fireEvent.change(input, { target: { value } });
};

describe('EditScheduledVirtualParticipant', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  it('opens with the stored values and an empty password field', () => {
    renderEditor();
    expect(screen.getByDisplayValue('Weekly sync')).toBeTruthy();
    expect(screen.getByDisplayValue('42')).toBeTruthy();
    expect(screen.getByPlaceholderText('Leave empty to keep the current password').value).toBe('');
  });

  it('saves only the changed field through updateScheduledVirtualParticipant', async () => {
    graphqlMock.mockResolvedValue({ data: { updateScheduledVirtualParticipant: { id: 'vp-1', meetingName: 'New' } } });
    const { onSaved } = renderEditor();

    typeInto('Enter meeting name', 'New');
    fireEvent.click(screen.getByText('Save changes'));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ id: 'vp-1', meetingName: 'New' }, true));
    const call = graphqlMock.mock.calls.find(([request]) => /updateScheduledVirtualParticipant/.test(request.query));
    expect(call[0].variables).toEqual({ input: { id: 'vp-1', meetingName: 'New' } });
  });

  it('shows why a save was refused, and keeps the form open', async () => {
    graphqlMock.mockRejectedValue({
      errors: [{ message: 'Virtual Participant vp-1 changed while it was being edited, and may have started' }],
    });
    const { onSaved } = renderEditor();

    typeInto('Enter meeting name', 'New');
    fireEvent.click(screen.getByText('Save changes'));

    expect(await screen.findByText(/changed while it was being edited/)).toBeTruthy();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('closes without a request when nothing changed', () => {
    const { onDismiss } = renderEditor();
    fireEvent.click(screen.getByText('Save changes'));
    expect(onDismiss).toHaveBeenCalled();
    expect(graphqlMock).not.toHaveBeenCalled();
  });
});

describe('the Edit action on Launch settings', () => {
  it('is rendered in the section header when supplied', () => {
    render(<LaunchSettings vp={SCHEDULED} headerActions={<button type="button">Edit</button>} />);
    expect(screen.getByText('Edit')).toBeTruthy();
  });
});
