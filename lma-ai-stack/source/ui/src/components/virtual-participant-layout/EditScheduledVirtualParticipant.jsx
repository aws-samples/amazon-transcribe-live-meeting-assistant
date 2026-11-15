/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Alert, Box, Button, Checkbox, Form, FormField, Modal, SpaceBetween } from '@cloudscape-design/components';

import useMeetingConsentDefaults from '../../hooks/use-meeting-consent-defaults';
import VirtualParticipantFormFields from './VirtualParticipantFormFields';
import ZoomCredentialsManager from './ZoomCredentialsManager';
import { meetingConsentChoiceAvailable } from './meeting-consent-options';
import { EDIT_MIN_LEAD_MINUTES, formFieldsAreValid, meetingTimeError } from './vp-form';
import { editFormFrom, movesLaunchSchedule, scheduledEditInput } from './scheduled-edit';

const client = generateClient();
const logger = new ConsoleLogger('EditScheduledVirtualParticipant');

export const updateScheduledVirtualParticipantMutation = /* GraphQL */ `
  mutation UpdateScheduledVirtualParticipant($input: UpdateScheduledVirtualParticipantInput!) {
    updateScheduledVirtualParticipant(input: $input) {
      id
      meetingName
      meetingPlatform
      meetingId
      meetingTime
      scheduledFor
      isScheduled
      status
      updatedAt
      enableVideoRecording
      agenda
      agendaSource
      consentMode
      consentRule
      scheduledTimeZone
      useStoredZoomAccount
      hasMeetingPassword
    }
  }
`;

const errorText = (err) =>
  err?.errors?.[0]?.message || err?.message || 'The changes could not be saved. Reload the page and try again.';

/**
 * The Create Virtual Participant form's fields, pre-filled from a scheduled
 * Virtual Participant, for changing it before it launches. `isOwner` says
 * whether the viewer scheduled it: only then is their own stored Zoom account
 * shown, though the account used is always the owner's.
 */
const EditScheduledVirtualParticipant = ({ vp, visible, onDismiss, onSaved, zoomSdkEnabled = false, isOwner }) => {
  const [state, setState] = useState(() => editFormFrom(vp));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [zoomCredsPresent, setZoomCredsPresent] = useState(false);

  // Start from the stored values each time the form opens; an update to the
  // row while it is open does not discard what is being typed.
  useEffect(() => {
    if (visible) {
      setState(editFormFrom(vp));
      setError('');
    }
  }, [visible]);

  const consentDefaults = useMeetingConsentDefaults(visible);
  // A consent choice the platform cannot support, or the deployment would not
  // use, goes back to the default.
  useEffect(() => {
    if (!meetingConsentChoiceAvailable(state.consentChoice, state.form.meetingPlatform, consentDefaults)) {
      setState((prev) => ({ ...prev, consentChoice: 'default' }));
    }
  }, [state.form.meetingPlatform, state.consentChoice, consentDefaults]);

  const handleZoomCredsChange = useCallback((status) => setZoomCredsPresent(!!status?.present), []);

  const { form } = state;
  const timeError =
    meetingTimeError(form.meetingDate, form.meetingTime, EDIT_MIN_LEAD_MINUTES) ||
    (!form.meetingDate || !form.meetingTime ? 'A scheduled Virtual Participant needs a meeting date and time.' : '');
  const valid = formFieldsAreValid(form) && !timeError;
  const zoomChoiceOffered = !zoomSdkEnabled;

  const handleSave = async () => {
    const input = scheduledEditInput(vp, state, { zoomChoiceOffered });
    if (!input) {
      onDismiss();
      return;
    }
    setSaving(true);
    setError('');
    try {
      const result = await client.graphql({ query: updateScheduledVirtualParticipantMutation, variables: { input } });
      onSaved(result?.data?.updateScheduledVirtualParticipant || null, movesLaunchSchedule(input));
    } catch (err) {
      logger.error('Failed to update scheduled Virtual Participant', err);
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const zoomAccountField = (
    <FormField
      label="Zoom account (Optional)"
      description={
        isOwner
          ? 'Stored Zoom credentials let LMA join meetings as authenticated user.'
          : 'The stored Zoom account is that of the user who scheduled this meeting.'
      }
      stretch
    >
      <SpaceBetween direction="vertical" size="s">
        {isOwner && <ZoomCredentialsManager onChange={handleZoomCredsChange} />}
        {(!isOwner || zoomCredsPresent || state.useZoomLogin) && (
          <Checkbox
            checked={state.useZoomLogin}
            onChange={({ detail }) => setState((prev) => ({ ...prev, useZoomLogin: detail.checked }))}
          >
            {isOwner
              ? 'Use my stored Zoom account when joining this meeting (recommended)'
              : "Use the scheduling user's stored Zoom account when joining this meeting"}
          </Checkbox>
        )}
      </SpaceBetween>
    </FormField>
  );

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Edit scheduled Virtual Participant"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleSave} disabled={!valid || saving} loading={saving}>
              Save changes
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form>
        <SpaceBetween direction="vertical" size="l">
          <Alert type="info">
            {`Changes can be made until ${EDIT_MIN_LEAD_MINUTES} minutes before the meeting time. ` +
              'If the Virtual Participant starts launching first, the changes are refused and it launches as ' +
              'it was scheduled.'}
          </Alert>
          {error && <Alert type="error">{error}</Alert>}
          <VirtualParticipantFormFields
            form={form}
            onChange={(patch) => setState((prev) => ({ ...prev, form: { ...prev.form, ...patch } }))}
            zoomSdkEnabled={zoomSdkEnabled}
            zoomAccountField={zoomAccountField}
            meetingTimeError={form.meetingTime ? timeError : ''}
            meetingTimeLabel="Meeting Time"
            meetingTimeDescription={`At least ${EDIT_MIN_LEAD_MINUTES} minutes from now, in your time zone.`}
            passwordLabel="Meeting Password (Optional)"
            passwordPlaceholder={
              vp.hasMeetingPassword ? 'Leave empty to keep the current password' : 'Enter meeting password if required'
            }
            consentChoice={state.consentChoice}
            onConsentChoiceChange={(value) => setState((prev) => ({ ...prev, consentChoice: value }))}
            consentDefaults={consentDefaults}
            enableVideoRecording={state.enableVideoRecording}
            onEnableVideoRecordingChange={(checked) => setState((prev) => ({ ...prev, enableVideoRecording: checked }))}
          />
        </SpaceBetween>
      </Form>
    </Modal>
  );
};

EditScheduledVirtualParticipant.propTypes = {
  vp: PropTypes.shape({
    id: PropTypes.string.isRequired,
    hasMeetingPassword: PropTypes.bool,
  }).isRequired,
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  onSaved: PropTypes.func.isRequired,
  zoomSdkEnabled: PropTypes.bool,
  isOwner: PropTypes.bool.isRequired,
};

export default EditScheduledVirtualParticipant;
