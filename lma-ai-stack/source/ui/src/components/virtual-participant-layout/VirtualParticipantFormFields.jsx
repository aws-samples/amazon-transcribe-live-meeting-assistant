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
  ExpandableSection,
  FormField,
  Input,
  Select,
  Textarea,
  TimeInput,
  Toggle,
  SpaceBetween,
} from '@cloudscape-design/components';

import { MAX_AGENDA_CHARS } from '../common/agenda';
import {
  AUDIENCE_OPTIONS,
  EMPTY_VP_PREPARATION,
  MAX_CONTEXT_CHARS,
  checklistProblem,
  optionFor,
  parseLabelsText,
} from '../common/meeting-preparation';
import { ChecklistEditor } from '../call-panel/ChecklistPanel';
import { MEETING_CONSENT_OPTIONS, meetingConsentOptionsFor } from './meeting-consent-options';
import { isMeetingUrl, zoomMeetingLinkHint } from './launch-settings';
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
  passwordDescription = '',
  // Join again's Continue: the meeting keeps the Virtual Participant's saved
  // settings, so only the platform and meeting ID can be edited (which starts
  // a new meeting instead), and there is no time to choose.
  settingsLocked = false,
  consentChoice,
  onConsentChoiceChange,
  consentDefaults = null,
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
  // The meeting preparation, checked as the resolver checks it.
  const preparation = form.preparation || EMPTY_VP_PREPARATION;
  const setPreparation = (patch) => onChange({ preparation: { ...preparation, ...patch } });
  const labelsError = parseLabelsText(preparation.labelsText).error;
  const contextTooLong = (preparation.meetingContext || '').length > MAX_CONTEXT_CHARS;
  const checklistError = checklistProblem(preparation.checklist);
  const preparationGiven = Boolean(
    preparation.labelsText || preparation.audience || preparation.meetingContext || preparation.checklist?.length,
  );
  // A choice the platform or the deployment rules out is shown disabled, with the reason.
  const consentOptions = meetingConsentOptionsFor(form.meetingPlatform, consentDefaults);

  return (
    <>
      <FormField label="Meeting Name" stretch>
        <Input
          value={form.meetingName}
          onChange={({ detail }) => onChange({ meetingName: detail.value })}
          placeholder="Enter meeting name"
          disabled={settingsLocked}
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
            : zoomMeetingLinkHint(form.meetingPlatform, form.meetingId) || undefined
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
        <FormField label={passwordLabel} description={passwordDescription || undefined} stretch>
          <Input
            value={form.meetingPassword}
            onChange={({ detail }) => onChange({ meetingPassword: detail.value })}
            placeholder={passwordPlaceholder}
            disabled={settingsLocked}
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

      {!settingsLocked && (
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
      )}

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
          disabled={settingsLocked}
        />
      </FormField>

      {/* A continued meeting keeps the preparation it has; Join again's Continue locks the settings. */}
      {!settingsLocked && (
        <ExpandableSection
          headerText="Meeting preparation (Optional)"
          headerDescription={
            'Labels, audience, a briefing for the assistant and the summary, and a checklist of what the ' +
            'meeting should cover. Stored on the meeting and editable on its page; never posted in the meeting chat.'
          }
          defaultExpanded={preparationGiven}
        >
          <SpaceBetween size="m">
            <FormField label="Audience" description="Whether people outside your organization are in the meeting.">
              <Select
                selectedOption={optionFor(AUDIENCE_OPTIONS, preparation.audience)}
                options={AUDIENCE_OPTIONS}
                onChange={({ detail }) => setPreparation({ audience: detail.selectedOption.value })}
                ariaLabel="Audience"
              />
            </FormField>
            <FormField
              label="Labels"
              description="One key=value per line, for example project=website-relaunch, to find it by."
              errorText={labelsError}
              stretch
            >
              <Textarea
                value={preparation.labelsText}
                onChange={({ detail }) => setPreparation({ labelsText: detail.value })}
                rows={3}
                ariaLabel="Labels, one key=value per line"
              />
            </FormField>
            <FormField
              label="Meeting context"
              description="Goals, background, terms and what a good outcome looks like, in Markdown."
              constraintText={
                contextTooLong ? `The meeting context is limited to ${MAX_CONTEXT_CHARS} characters.` : undefined
              }
              stretch
            >
              <Textarea
                value={preparation.meetingContext}
                onChange={({ detail }) => setPreparation({ meetingContext: detail.value })}
                rows={5}
                ariaLabel="Meeting context, Markdown"
                invalid={contextTooLong}
              />
            </FormField>
            <FormField
              label="Checklist"
              description="Questions to ask, topics, risks to probe and things to confirm. Every item starts open."
              errorText={preparation.checklist?.length ? checklistError : null}
              stretch
            >
              <ChecklistEditor
                draft={preparation.checklist || []}
                setDraft={(checklist) => setPreparation({ checklist })}
              />
            </FormField>
          </SpaceBetween>
        </ExpandableSection>
      )}

      <FormField
        label="Meeting consent"
        description={
          'Whether participants are told the meeting is recorded and transcribed, and whether the Virtual ' +
          'Participant waits for their consent first. When the deployment does not allow a less strict ' +
          'choice than its own, those choices are shown but cannot be chosen. Once recording has been withheld ' +
          'after it started (an objection, or a newcomer who has not consented yet), the screen recording stays ' +
          'off for the rest of the meeting; the transcript and audio recording resume.'
        }
        stretch
      >
        <Select
          selectedOption={consentOptions.find((option) => option.value === consentChoice) || MEETING_CONSENT_OPTIONS[0]}
          onChange={({ detail }) => onConsentChoiceChange(detail.selectedOption.value)}
          options={consentOptions}
          ariaLabel="Meeting consent"
          disabled={settingsLocked}
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
        <Toggle
          checked={enableVideoRecording}
          onChange={({ detail }) => onEnableVideoRecordingChange(detail.checked)}
          disabled={settingsLocked}
        >
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
    preparation: PropTypes.shape({
      labelsText: PropTypes.string,
      audience: PropTypes.string,
      meetingContext: PropTypes.string,
      checklist: PropTypes.arrayOf(PropTypes.shape({ ItemId: PropTypes.string })),
    }),
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
  passwordDescription: PropTypes.string,
  settingsLocked: PropTypes.bool,
  consentChoice: PropTypes.string.isRequired,
  onConsentChoiceChange: PropTypes.func.isRequired,
  consentDefaults: PropTypes.shape({
    Mode: PropTypes.string,
    Rule: PropTypes.string,
    AllowPerMeetingOverride: PropTypes.bool,
  }),
  enableVideoRecording: PropTypes.bool.isRequired,
  onEnableVideoRecordingChange: PropTypes.func.isRequired,
};

export default VirtualParticipantFormFields;
