/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import LaunchSettings from './LaunchSettings';
import EditScheduledVirtualParticipant from './EditScheduledVirtualParticipant';

// generateClient() runs at module load in the modal and in the Zoom and
// browser-profile managers; every GraphQL call goes through graphqlMock.
const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    error() {} // eslint-disable-line class-methods-use-this

    warn() {} // eslint-disable-line class-methods-use-this
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

  it('closes without saving when nothing changed', () => {
    const { onDismiss } = renderEditor();
    fireEvent.click(screen.getByText('Save changes'));
    expect(onDismiss).toHaveBeenCalled();
    const saves = graphqlMock.mock.calls.filter(([request]) => /updateScheduledVirtualParticipant/.test(request.query));
    expect(saves).toEqual([]);
  });
});

// The select in the form field labelled Meeting consent.
const consentSelect = () =>
  createWrapper(document.body)
    .findAllFormFields()
    .find((field) => field.findLabel()?.getElement().textContent === 'Meeting consent')
    .findControl()
    .findSelect();

// openDropdown toggles, so it is opened only when no option is showing.
const consentOptions = () => {
  const select = consentSelect();
  if (select.findDropdown().findOptions().length === 0) select.openDropdown();
  return select
    .findDropdown()
    .findOptions()
    .map((option) => ({
      text: option.getElement().textContent,
      disabled: option.isDisabled(),
    }));
};

const answerDefaults = (defaults) =>
  graphqlMock.mockImplementation(async ({ query }) => {
    if (/getMeetingConsentDefaults/.test(query)) {
      if (defaults instanceof Error) throw defaults;
      return { data: { getMeetingConsentDefaults: defaults } };
    }
    return { data: {} };
  });

// Once the form has read the settings and rendered what it read.
const settingsRead = async () => {
  await waitFor(() =>
    expect(graphqlMock.mock.calls.some(([request]) => /getMeetingConsentDefaults/.test(request.query))).toBe(true),
  );
  await act(async () => {});
};

describe('the Meeting consent choice', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  it('disables each choice the deployment would not use, and keeps the stricter ones', async () => {
    answerDefaults({ Mode: 'notify', Rule: 'each_participant', AllowPerMeetingOverride: false });
    renderEditor();
    await settingsRead();
    const options = consentOptions();
    expect(options.find((o) => /^Off/.test(o.text)).disabled).toBe(true);
    expect(options.find((o) => /^Off/.test(o.text)).text).toMatch(
      /Not allowed in this deployment: the meeting would use Notify/,
    );
    expect(options.find((o) => /^The deployment's setting/.test(o.text)).disabled).toBe(false);
    expect(options.find((o) => /^Notify/.test(o.text)).disabled).toBe(false);
    expect(options.filter((o) => /^Opt-in/.test(o.text)).map((o) => o.disabled)).toEqual([false, false]);
  });

  it('moves a stored choice the deployment would not use back to its setting', async () => {
    answerDefaults({ Mode: 'opt_in', Rule: 'owner_attests', AllowPerMeetingOverride: false });
    renderEditor({ vp: { ...SCHEDULED, consentMode: 'notify' } });
    expect(consentSelect().findTrigger().getElement().textContent).toMatch(/^Notify/);
    await settingsRead();
    expect(consentSelect().findTrigger().getElement().textContent).toMatch(/The deployment's setting/);
  });

  it('offers every choice when the settings cannot be read', async () => {
    answerDefaults(new Error('the meeting consent settings could not be read; try again'));
    renderEditor({ vp: { ...SCHEDULED, consentMode: 'off' } });
    await settingsRead();
    expect(consentSelect().findTrigger().getElement().textContent).toMatch(/^Off/);
    expect(consentOptions().map((o) => o.disabled)).toEqual([false, false, false, false, false]);
  });

  it('offers every choice when the deployment allows a per-meeting choice', async () => {
    answerDefaults({ Mode: 'opt_in', Rule: 'each_participant', AllowPerMeetingOverride: true });
    renderEditor();
    await settingsRead();
    const options = consentOptions();
    expect(options[0].text).toMatch(/This deployment uses Opt-in, waiting until every participant consents/);
    expect(options.map((o) => o.disabled)).toEqual([false, false, false, false, false]);
  });
});

describe('the Edit action on Launch settings', () => {
  it('is rendered in the section header when supplied', () => {
    render(<LaunchSettings vp={SCHEDULED} headerActions={<button type="button">Edit</button>} />);
    expect(screen.getByText('Edit')).toBeTruthy();
  });
});
