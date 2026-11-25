/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The share dialog saves on condition of the sharing list it read: it sends
 * each meeting's list as ExpectedSharedWith, and when shareMeetings answers
 * that the list changed meanwhile it says so and shows the list as it is now.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const api = vi.hoisted(() => ({ calls: [], answer: null }));

vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({
    graphql: (options) => {
      api.calls.push(options);
      return api.answer(options);
    },
  }),
}));

// eslint-disable-next-line import/first
import { shareModal, SHARING_LIST_CHANGED_MESSAGE } from './meeting-controls';

const CALL = {
  CallId: 'm1',
  ListPK: 'cls#2026-11-24#s#04',
  ListSK: 'ts#2026-11-24T17:05:00.000Z#id#m1',
  CreatedAt: '2026-11-24T17:05:00.000Z',
};

const Harness = (props) => shareModal(props);

// What the Amplify client rejects with when the response carries GraphQL errors.
const graphqlError = (error) => Object.assign(new Error(error.message), { errors: [error] });

const renderDialog = (getCallDetailsFromCallIds) =>
  render(
    <MemoryRouter>
      <Harness
        calls={[CALL]}
        selectedItems={[{ callId: 'm1' }]}
        getCallDetailsFromCallIds={getCallDetailsFromCallIds}
      />
    </MemoryRouter>,
  );

const addAndSubmit = async (address) => {
  fireEvent.click(screen.getByRole('button', { name: 'Share' }));
  await screen.findByText('ana@example.com');
  const input = screen.getByPlaceholderText('Enter email addresses');
  fireEvent.change(input, { target: { value: address } });
  fireEvent.click(screen.getByRole('button', { name: 'Add' }));
  await screen.findByText(address);
  fireEvent.click(screen.getByRole('button', { name: 'Submit' }));
};

beforeEach(() => {
  api.calls = [];
});

describe('the share dialog saves on condition of the list it read', () => {
  it('sends each meeting the list it read as ExpectedSharedWith', async () => {
    api.answer = () => Promise.resolve({ data: { shareMeetings: { Result: 'Meetings shared successfully' } } });
    const details = vi.fn().mockResolvedValue([{ CallId: 'm1', SharedWith: '[ana@example.com]' }]);
    renderDialog(details);
    await addAndSubmit('ben@example.com');
    await screen.findByText('Meetings shared successfully');
    const { input } = api.calls[0].variables;
    expect(input.MeetingRecipients).toBe('ana@example.com,ben@example.com');
    expect(input.Calls).toEqual([
      { CallId: 'm1', ListPK: CALL.ListPK, ListSK: CALL.ListSK, ExpectedSharedWith: ['ana@example.com'] },
    ]);
  });

  it('says the list changed, and shows the current list, when shareMeetings refuses', async () => {
    api.answer = () =>
      Promise.reject(
        graphqlError({
          errorType: 'SharingListChanged',
          message: 'The sharing list changed; reopen to see the current list',
        }),
      );
    const details = vi
      .fn()
      .mockResolvedValueOnce([{ CallId: 'm1', SharedWith: '[ana@example.com]' }])
      .mockResolvedValue([{ CallId: 'm1', SharedWith: '[ana@example.com, carl@example.com]' }]);
    renderDialog(details);
    await addAndSubmit('ben@example.com');

    await screen.findByText(SHARING_LIST_CHANGED_MESSAGE);
    // Read again: the share made meanwhile is shown, and the user being added is kept.
    await screen.findByText('carl@example.com');
    expect(details).toHaveBeenCalledTimes(2);
    expect(screen.getByText('ben@example.com')).toBeTruthy();
    expect(screen.queryByText('Meetings shared successfully')).toBeNull();
    // Submitting again sends the list read the second time.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit' }).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));
    await waitFor(() => expect(api.calls).toHaveLength(2));
    expect(api.calls[1].variables.input.Calls[0].ExpectedSharedWith).toEqual(['ana@example.com', 'carl@example.com']);
  });

  it('shows any other failure instead of leaving the dialog waiting', async () => {
    api.answer = () => Promise.reject(graphqlError({ message: 'Request failed' }));
    const details = vi.fn().mockResolvedValue([{ CallId: 'm1', SharedWith: '[ana@example.com]' }]);
    renderDialog(details);
    await addAndSubmit('ben@example.com');
    await screen.findByText('Request failed');
    expect(screen.queryByText(SHARING_LIST_CHANGED_MESSAGE)).toBeNull();
  });
});
