/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Configuration History page against a mocked GraphQL client.
 *
 * What is asserted is what would be silent if wrong: a restore carries the
 * version the page loaded (or it replaces a save made since), the current
 * revision cannot be "restored" onto itself, and a refused restore says to
 * refresh rather than to try again.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigurationHistoryContent } from './ConfigurationHistoryPage';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));

const LISTING = {
  data: {
    listConfigurationRevisions: {
      Profile: 'custom',
      Version: 7,
      LatestRevision: 3,
      PublishedRevision: 3,
      Revisions: [
        {
          Revision: 3,
          CreatedAt: '2026-10-31T10:00:00Z',
          CreatedBy: 'second@example.com',
          Sections: ['asr_engine'],
          Comment: null,
          Source: 'save',
          RestoredFrom: null,
          Label: null,
          Version: 7,
          Published: true,
        },
        {
          Revision: 2,
          CreatedAt: '2026-10-30T10:00:00Z',
          CreatedBy: 'first@example.com',
          Sections: ['nova_sonic_config'],
          Comment: 'deeper voice',
          Source: 'save',
          RestoredFrom: null,
          Label: null,
          Version: 6,
          Published: false,
        },
      ],
    },
  },
};

const operationOf = (call) => call[0].query.match(/(query|mutation) (\w+)/)[2];

// Cloudscape renders a selection checkbox per row, labelled by nothing a test
// can name, so a row is selected through the checkbox in the row that holds
// the text.
const selectRow = (text) => {
  const row = screen.getByText(text).closest('tr');
  fireEvent.click(within(row).getByRole('checkbox'));
};

// A page action. Cloudscape keeps a closed dialog's buttons in the document, and
// the restore dialog has a "Restore" button of its own; the page's action comes
// first in document order.
const button = (name) => screen.getAllByRole('button', { name })[0];

describe('ConfigurationHistoryContent', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  it('lists who changed what, and marks the current revision', async () => {
    graphqlMock.mockResolvedValue(LISTING);

    render(<ConfigurationHistoryContent />);

    expect(await screen.findByText('first@example.com')).toBeTruthy();
    expect(screen.getByText('deeper voice')).toBeTruthy();
    expect(screen.getByText('Nova Sonic')).toBeTruthy();
    expect(screen.getByText('Current')).toBeTruthy();
  });

  it('does not offer to restore the revision that is already current', async () => {
    graphqlMock.mockResolvedValue(LISTING);
    render(<ConfigurationHistoryContent />);
    await screen.findByText('second@example.com');

    selectRow('second@example.com');

    expect(button('Restore').disabled).toBe(true);
  });

  it('restores against the version it loaded, with the comment given', async () => {
    graphqlMock.mockImplementation(async (request) =>
      request.query.includes('mutation RestoreConfigurationRevision')
        ? { data: { restoreConfigurationRevision: { Success: true, Version: 8, Revision: 4, Unchanged: false } } }
        : LISTING,
    );
    render(<ConfigurationHistoryContent />);
    await screen.findByText('first@example.com');

    selectRow('first@example.com');
    fireEvent.click(button('Restore'));
    const dialog = await screen.findByRole('dialog', { name: /Restore revision 2/ });
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'back to the old voice' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));

    await screen.findByText(/Restored revision 2 as revision 4/);
    const restore = graphqlMock.mock.calls.find((call) => operationOf(call) === 'RestoreConfigurationRevision');
    expect(restore[0].variables.input).toEqual({
      Profile: 'custom',
      Revision: 2,
      ExpectedVersion: 7,
      Comment: 'back to the old voice',
    });
  });

  it('tells the admin to refresh when a save landed after the page loaded', async () => {
    graphqlMock.mockImplementation(async (request) => {
      if (request.query.includes('mutation RestoreConfigurationRevision')) {
        throw Object.assign(new Error('GraphQL error'), {
          errors: [{ message: 'Conflict: the configuration profile was saved by someone' }],
        });
      }
      return LISTING;
    });
    render(<ConfigurationHistoryContent />);
    await screen.findByText('first@example.com');

    selectRow('first@example.com');
    fireEvent.click(button('Restore'));
    const dialog = await screen.findByRole('dialog', { name: /Restore revision 2/ });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));

    expect(await screen.findByText(/Refresh the history to see their change/)).toBeTruthy();
  });

  it('keeps the dialog and the comment when a restore fails for another reason', async () => {
    graphqlMock.mockImplementation(async (request) => {
      if (request.query.includes('mutation RestoreConfigurationRevision')) {
        throw new Error('Network error');
      }
      return LISTING;
    });
    render(<ConfigurationHistoryContent />);
    await screen.findByText('first@example.com');

    selectRow('first@example.com');
    fireEvent.click(button('Restore'));
    const dialog = await screen.findByRole('dialog', { name: /Restore revision 2/ });
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'typed once' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));

    expect(await within(dialog).findByText(/Could not restore revision 2: Network error/)).toBeTruthy();
    expect(within(dialog).getByRole('textbox').value).toBe('typed once');
  });

  it('labels a revision, and clears a label left empty', async () => {
    graphqlMock.mockImplementation(async (request) =>
      request.query.includes('mutation LabelConfigurationRevision')
        ? { data: { labelConfigurationRevision: { Success: true, Revision: 2 } } }
        : LISTING,
    );
    render(<ConfigurationHistoryContent />);
    await screen.findByText('first@example.com');

    selectRow('first@example.com');
    fireEvent.click(button('Label'));
    let dialog = await screen.findByRole('dialog', { name: /Label revision 2/ });
    fireEvent.change(within(dialog).getByLabelText('Label'), { target: { value: '  before the launch  ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save label' }));
    await screen.findByText(/Labelled revision 2 "before the launch"/);

    selectRow('first@example.com');
    fireEvent.click(button('Label'));
    dialog = await screen.findByRole('dialog', { name: /Label revision 2/ });
    fireEvent.change(within(dialog).getByLabelText('Label'), { target: { value: '' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save label' }));
    await screen.findByText(/Removed the label from revision 2/);

    const labels = graphqlMock.mock.calls.filter((call) => operationOf(call) === 'LabelConfigurationRevision');
    expect(labels.map((call) => call[0].variables.input)).toEqual([
      { Profile: 'custom', Revision: 2, Label: 'before the launch' },
      { Profile: 'custom', Revision: 2, Label: null },
    ]);
  });

  it('compares two revisions from the older to the newer', async () => {
    graphqlMock.mockImplementation(async (request) =>
      request.query.includes('query DiffConfigurationRevisions')
        ? {
            data: {
              diffConfigurationRevisions: {
                FromRevision: 2,
                ToRevision: 3,
                Changes: JSON.stringify([
                  { Path: ['asr_engine', 'streamingEngineMicrovm'], Change: 'changed', Before: false, After: true },
                ]),
              },
            },
          }
        : LISTING,
    );
    render(<ConfigurationHistoryContent />);
    await screen.findByText('first@example.com');

    selectRow('second@example.com');
    selectRow('first@example.com');
    fireEvent.click(button('Compare'));

    expect(await screen.findByText('Transcription engine › streamingEngineMicrovm')).toBeTruthy();
    await waitFor(() => {
      const diff = graphqlMock.mock.calls.find((call) => operationOf(call) === 'DiffConfigurationRevisions');
      expect(diff[0].variables).toEqual({ Profile: 'custom', FromRevision: 2, ToRevision: 3 });
    });
  });
});
