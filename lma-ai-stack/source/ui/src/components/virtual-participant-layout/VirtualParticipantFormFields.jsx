/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  DatePicker,
  FormField,
  Input,
  Select,
  Textarea,
  TimeInput,
  Toggle,
  SpaceBetween,
} from '@cloudscape-design/components';

import { MAX_AGENDA_CHARS } from '../common/agenda';
import { MEETING_CONSENT_OPTIONS, meetingConsentOptionsFor } from './meeting-consent-options';
import { isMeetingUrl } from './launch-settings';
import { PLATFORM_OPTIONS, agendaIsTooLong, meetingIdError } from './vp-form';

/**
 * The fields of the Virtual Participant form, from the meeting name to video
 * recording, shared by the Create Virtual Participant form and the form that
 * edits a scheduled Virtual Participant. The parent owns the values; this
 * component renders them and reports changes through `onChange(patch)`.
 *
 * `zoomAccountField` is rendered for a Zoom meeting when the deployment does
 * not join through the Zoom Meeting SDK, and `browserProfileField` after it;
 * each form supplies its own.
 */
const VirtualParticipantFormFields = ({
  form,
  onChange,
  zoomSdkEnabled = false,
  zoomAccountField = null,
  browserProfileField = null,
  meetingIdNote = '',
  onMeetingIdNoteClear = () => {},
  meetingTimeError = '',
  meetingTimeLabel = 'Meeting Time (Optional)',
  meetingTimeDescription = 'Choose a date and time that is at least two minutes from now to schedule the participant.',
  passwordLabel = 'Meeting Password (Optional)',
  passwordPlaceholder = 'Enter meeting password if required',
  consentChoice,
  onConsentChoiceChange,
  enableVideoRecording,
  onEnableVideoRecordingChange,
}) => {
  // A meetingId that is a full join URL (http/https) carries its own password
  // server-side, so the field is relabelled and the password input hidden.
  const meetingIdIsUrl = isMeetingUrl(form.meetingId);
  // Google Meet: a meeting code or link, and never a password.
  const isGoogleMeetForm = form.meetingPlatform === 'GOOGLE_MEET';
  const idError = meetingIdError(form);
  let meetingIdLabel = meetingIdIsUrl ? 'Meeting URL' : 'Meeting ID';
  let meetingIdPlaceholder = meetingIdIsUrl ? 'Enter meeting URL' : 'Enter meeting ID or paste a meeting URL';
  if (isGoogleMeetForm) {
    meetingIdLabel = 'Meeting code or link';
    meetingIdPlaceholder = 'abc-defg-hij or https://meet.google.com/abc-defg-hij';
  }
  // The resolver refuses a longer agenda, so the form does not offer to submit one.
  const agendaTooLong = agendaIsTooLong(form.agenda);

  return (
    <>
      <FormField label="Meeting Name" stretch>
        <Input
          value={form.meetingName}
          onChange={({ detail }) => onChange({ meetingName: detail.value })}
          placeholder="Enter meeting name"
        />
      </FormField>

      <FormField label="Meeting Platform" stretch>
        <Select
          selectedOption={PLATFORM_OPTIONS.find((opt) => opt.value === form.meetingPlatform)}
          onChange={({ detail }) => {
            onMeetingIdNoteClear();
            onChange({ meetingPlatform: detail.selectedOption.value });
          }}
          options={PLATFORM_OPTIONS}
        />
      </FormField>

      {/* A full join URL (e.g. a Webex "j.php?MTID=..." launch link) carries
          the meeting and password server-side, so relabel the field "Meeting
          URL" and hide the separate password input when a URL is entered. A
          bare numeric ID keeps the "Meeting ID" + password layout. */}
      <FormField
        label={meetingIdLabel}
        description={
          isGoogleMeetForm
            ? 'The meeting has to accept guests who join from a browser without a Google account, and ' +
              'someone in it may need to admit the participant.'
            : undefined
        }
        errorText={idError || undefined}
        constraintText={(!form.meetingId && meetingIdNote) || undefined}
        stretch
      >
        <Input
          value={form.meetingId}
          onChange={({ detail }) => {
            onMeetingIdNoteClear();
            onChange({ meetingId: detail.value });
          }}
          placeholder={meetingIdPlaceholder}
        />
      </FormField>

      {!meetingIdIsUrl && !isGoogleMeetForm && (
        <FormField label={passwordLabel} stretch>
          <Input
            value={form.meetingPassword}
            onChange={({ detail }) => onChange({ meetingPassword: detail.value })}
            placeholder={passwordPlaceholder}
          />
        </FormField>
      )}

      {form.meetingPlatform === 'ZOOM' && zoomSdkEnabled && (
        <FormField label="Zoom account" stretch>
          <Alert type="info">
            Joining via the Zoom Meeting SDK. No Zoom account sign-in is needed — the participant authenticates with a
            meeting signature.
          </Alert>
        </FormField>
      )}

      {form.meetingPlatform === 'ZOOM' && !zoomSdkEnabled && zoomAccountField}

      {browserProfileField}

      <FormField label={meetingTimeLabel} description={meetingTimeDescription} stretch>
        <SpaceBetween direction="horizontal" size="l">
          <DatePicker
            onChange={({ detail }) => onChange({ meetingDate: detail.value })}
            value={form.meetingDate}
            isDateEnabled={(date) => {
              const currentDate = new Date();
              currentDate.setDate(currentDate.getDate() - 1);
              return date > currentDate;
            }}
            placeholder="YYYY/MM/DD"
          />
          <TimeInput
            onChange={({ detail }) => onChange({ meetingTime: detail.value })}
            value={form.meetingTime}
            disabled={form.meetingDate.length !== 10}
            format="hh:mm"
            placeholder="hh:mm (24-hour format)"
            use24Hour
          />
        </SpaceBetween>
        {meetingTimeError && <Alert type="error">{meetingTimeError}</Alert>}
      </FormField>

      <FormField
        label="Agenda (Optional)"
        description={
          'One item per line. It is stored on the meeting, shown on its page and can be edited there ' +
          'during and after the meeting.'
        }
        constraintText={agendaTooLong ? `The agenda is limited to ${MAX_AGENDA_CHARS} characters.` : undefined}
        stretch
      >
        <Textarea
          value={form.agenda || ''}
          onChange={({ detail }) => onChange({ agenda: detail.value })}
          placeholder={'Introductions\nQ3 budget review\nHiring plan'}
          rows={4}
          ariaLabel="Agenda, one item per line"
          invalid={agendaTooLong}
        />
      </FormField>

      <FormField
        label="Meeting consent"
        description={
          'Whether participants are told the meeting is recorded and transcribed, and whether the Virtual ' +
          'Participant waits for their consent first. The deployment may not allow a less strict choice than ' +
          'its own, in which case its own applies. Once recording has been withheld after it started (an ' +
          'objection, or a newcomer who has not consented yet), the screen recording stays off for the rest ' +
          'of the meeting; the transcript and audio recording resume.'
        }
        stretch
      >
        <Select
          selectedOption={
            meetingConsentOptionsFor(form.meetingPlatform).find((option) => option.value === consentChoice) ||
            MEETING_CONSENT_OPTIONS[0]
          }
          onChange={({ detail }) => onConsentChoiceChange(detail.selectedOption.value)}
          options={meetingConsentOptionsFor(form.meetingPlatform)}
          ariaLabel="Meeting consent"
        />
      </FormField>

      <FormField
        label="Video recording"
        description={
          'Capture the meeting screen (screen shares, slides, participants) as an MP4 alongside the audio ' +
          'recording. Adds CPU/storage cost. Turn off to record audio and transcript only.'
        }
        stretch
      >
        <Toggle checked={enableVideoRecording} onChange={({ detail }) => onEnableVideoRecordingChange(detail.checked)}>
          Record meeting video
        </Toggle>
      </FormField>
    </>
  );
};

VirtualParticipantFormFields.propTypes = {
  form: PropTypes.shape({
    meetingName: PropTypes.string,
    meetingPlatform: PropTypes.string,
    meetingId: PropTypes.string,
    meetingPassword: PropTypes.string,
    meetingDate: PropTypes.string,
    meetingTime: PropTypes.string,
    agenda: PropTypes.string,
  }).isRequired,
  onChange: PropTypes.func.isRequired,
  zoomSdkEnabled: PropTypes.bool,
  zoomAccountField: PropTypes.node,
  browserProfileField: PropTypes.node,
  meetingIdNote: PropTypes.string,
  onMeetingIdNoteClear: PropTypes.func,
  meetingTimeError: PropTypes.string,
  meetingTimeLabel: PropTypes.string,
  meetingTimeDescription: PropTypes.string,
  passwordLabel: PropTypes.string,
  passwordPlaceholder: PropTypes.string,
  consentChoice: PropTypes.string.isRequired,
  onConsentChoiceChange: PropTypes.func.isRequired,
  enableVideoRecording: PropTypes.bool.isRequired,
  onEnableVideoRecordingChange: PropTypes.func.isRequired,
};

export default VirtualParticipantFormFields;
