/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { fireEvent, render, screen, renderHook, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import VirtualParticipantFormFields, { VIDEO_RECORDING_OFF_TEXT } from './VirtualParticipantFormFields';
import LaunchSettings from './LaunchSettings';
import useVpVideoRecordingSetting from '../../hooks/use-vp-video-recording-setting';

vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: class {
    warn() {} // eslint-disable-line class-methods-use-this
  },
}));

const FORM = {
  meetingName: 'Weekly sync',
  meetingPlatform: 'ZOOM',
  meetingId: '123',
  meetingPassword: '',
  meetingDate: '',
  meetingTime: '',
  agenda: '',
};

const renderFields = (props) =>
  render(
    <VirtualParticipantFormFields
      form={FORM}
      onChange={() => {}}
      consentChoice="default"
      onConsentChoiceChange={() => {}}
      enableVideoRecording
      onEnableVideoRecordingChange={() => {}}
      {...props} // eslint-disable-line react/jsx-props-no-spreading
    />,
  );

const videoToggle = (container) => {
  const toggles = createWrapper(container).findAllToggles();
  return toggles.find((toggle) => toggle.getElement().textContent.includes('Record meeting video'));
};

describe('the Record meeting video switch', () => {
  it('is off and disabled, with the reason, when the setting is off', () => {
    const { container } = renderFields({ videoRecordingAllowed: false });
    const toggle = videoToggle(container);
    expect(toggle.findNativeInput().getElement().checked).toBe(false);
    expect(toggle.findNativeInput().getElement().disabled).toBe(true);
    expect(screen.getByText(VIDEO_RECORDING_OFF_TEXT)).toBeTruthy();
  });

  it.each([true, null])('is the user’s choice when the setting is %s', (allowed) => {
    const { container } = renderFields({ videoRecordingAllowed: allowed });
    const toggle = videoToggle(container);
    expect(toggle.findNativeInput().getElement().checked).toBe(true);
    expect(toggle.findNativeInput().getElement().disabled).toBe(false);
    expect(screen.queryByText(VIDEO_RECORDING_OFF_TEXT)).toBeNull();
  });
});

describe('useVpVideoRecordingSetting', () => {
  it('reads the setting when the form opens', async () => {
    const client = { graphql: vi.fn().mockResolvedValue({ data: { getVirtualParticipantVideoRecording: false } }) };
    const { result } = renderHook(() => useVpVideoRecordingSetting(true, { client }));
    await waitFor(() => expect(result.current).toBe(false));
    expect(client.graphql).toHaveBeenCalledTimes(1);
  });

  it('is null when the setting cannot be read, or the form is closed', async () => {
    const client = { graphql: vi.fn().mockRejectedValue(new Error('denied')) };
    const { result } = renderHook(() => useVpVideoRecordingSetting(true, { client }));
    await waitFor(() => expect(client.graphql).toHaveBeenCalled());
    expect(result.current).toBeNull();
    const closed = { graphql: vi.fn() };
    renderHook(() => useVpVideoRecordingSetting(false, { client: closed }));
    expect(closed.graphql).not.toHaveBeenCalled();
  });
});

describe('Launch settings', () => {
  it.each([
    ['configuration', /Record Virtual Participant meeting video is off on the Configuration page/],
    ['deployment', /this deployment does not record meeting video/],
  ])('say why the video is not recorded (%s)', (reason, text) => {
    render(
      <LaunchSettings
        vp={{ id: 'vp-1', meetingPlatform: 'ZOOM', enableVideoRecording: true, videoRecordingOffReason: reason }}
      />,
    );
    fireEvent.click(screen.getByText('Launch settings'));
    expect(screen.getByText(text)).toBeTruthy();
  });
});
