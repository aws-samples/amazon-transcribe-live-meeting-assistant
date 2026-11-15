/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  CopyToClipboard,
  FormField,
  Header,
  Input,
  RadioGroup,
  SpaceBetween,
  Textarea,
} from '@cloudscape-design/components';

import { CONSENT_METHODS, CONSENT_MODE_OPT_IN } from './consent';

/**
 * The consent step of a streamed meeting: the notice to read out or post, the
 * user's confirmation and how it was given, an optional evidence screenshot,
 * and, while streaming, a way to report that a participant objected.
 */
const ConsentStep = ({
  settings,
  noticeText,
  step,
  onStepChange,
  evidence,
  onCaptureEvidence,
  onClearEvidence,
  streaming,
  status = null,
  evidenceStatus = null,
  onObjection,
}) => {
  const [objectorName, setObjectorName] = useState('');
  const optIn = settings.mode === CONSENT_MODE_OPT_IN;
  const update = (change) => onStepChange({ ...step, ...change });

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={
            optIn
              ? 'This deployment records and transcribes a meeting only after everyone in it has agreed. ' +
                'Share the request below, then confirm that everyone agreed.'
              : 'This deployment tells everyone in a meeting that it is recorded and transcribed. ' +
                'Share the notice below; if anybody objects, stop the stream with Participant objected.'
          }
        >
          Meeting consent
        </Header>
      }
    >
      <SpaceBetween size="m">
        {settings.supported === false && (
          <Alert type="error" header="This meeting cannot be streamed here">
            {settings.reason}
          </Alert>
        )}
        <FormField
          label={optIn ? 'Consent request' : 'Notice'}
          description="Read it out, or paste it into the meeting's chat."
          stretch
        >
          <SpaceBetween size="xs">
            <Box variant="p">
              <span data-testid="consent-notice" style={{ whiteSpace: 'pre-wrap' }}>
                {noticeText}
              </span>
            </Box>
            <CopyToClipboard
              variant="button"
              textToCopy={noticeText}
              copyButtonText="Copy notice"
              copySuccessText="Notice copied"
              copyErrorText="The notice could not be copied"
            />
          </SpaceBetween>
        </FormField>

        <FormField
          label={optIn ? 'Confirmation (required)' : 'Confirmation'}
          description={
            optIn
              ? 'Recorded on the meeting as your attestation that everyone agreed.'
              : 'Recorded on the meeting as the notice having been given.'
          }
        >
          <Checkbox
            checked={step.confirmed}
            onChange={({ detail }) => update({ confirmed: detail.checked })}
            disabled={streaming}
          >
            {optIn
              ? 'Everyone in the meeting agreed to it being recorded and transcribed'
              : 'I have told everyone in the meeting, with the notice above'}
          </Checkbox>
        </FormField>

        <FormField label={optIn ? 'How everyone agreed' : 'How they were told'}>
          <RadioGroup
            value={step.method}
            onChange={({ detail }) => update({ method: detail.value })}
            items={CONSENT_METHODS.map((item) => ({ ...item, disabled: streaming }))}
          />
        </FormField>

        <FormField label="Note (optional)" description="Recorded with your confirmation." stretch>
          <Textarea
            value={step.statement}
            onChange={({ detail }) => update({ statement: detail.value })}
            rows={2}
            disabled={streaming}
            placeholder="For example: everyone agreed at the start of the call."
          />
        </FormField>

        <FormField
          label="Evidence screenshot (optional)"
          description={
            streaming
              ? 'Keeps one frame of the tab you are streaming, for example the chat after everyone replied.'
              : "Pick the meeting's tab or window; one frame is kept on the meeting as evidence and checked."
          }
        >
          <SpaceBetween size="xs" direction="horizontal">
            <Button iconName="camera" onClick={onCaptureEvidence} loading={evidence.capturing}>
              Capture evidence
            </Button>
            {evidence.dataUrl && !streaming && (
              <Button variant="link" onClick={onClearEvidence}>
                Remove
              </Button>
            )}
          </SpaceBetween>
          {evidence.dataUrl && (
            <Box margin={{ top: 'xs' }}>
              <img
                src={evidence.dataUrl}
                alt="Evidence screenshot to be kept on the meeting"
                style={{ maxWidth: '320px', border: '1px solid #ccc' }}
              />
            </Box>
          )}
          {evidenceStatus && (
            <Box
              margin={{ top: 'xs' }}
              color={evidenceStatus.type === 'error' ? 'text-status-error' : 'text-body-secondary'}
            >
              {evidenceStatus.text}
            </Box>
          )}
          {evidence.error && (
            <Box color="text-status-error" margin={{ top: 'xs' }}>
              {evidence.error}
            </Box>
          )}
        </FormField>

        {status && (
          <Alert type={status.type} data-testid="consent-status">
            {status.text}
          </Alert>
        )}

        {streaming && (
          <FormField
            label="If a participant objects"
            description="Stops recording and transcription now, and records the objection on the meeting."
          >
            <SpaceBetween size="xs" direction="horizontal">
              <Input
                value={objectorName}
                onChange={({ detail }) => setObjectorName(detail.value)}
                placeholder="Their name (optional)"
                ariaLabel="Name of the participant who objected"
              />
              <Button iconName="status-negative" onClick={() => onObjection(objectorName.trim())}>
                Participant objected
              </Button>
            </SpaceBetween>
          </FormField>
        )}
      </SpaceBetween>
    </Container>
  );
};

ConsentStep.propTypes = {
  settings: PropTypes.shape({
    mode: PropTypes.string,
    supported: PropTypes.bool,
    reason: PropTypes.string,
  }).isRequired,
  noticeText: PropTypes.string.isRequired,
  step: PropTypes.shape({
    confirmed: PropTypes.bool,
    method: PropTypes.string,
    statement: PropTypes.string,
  }).isRequired,
  onStepChange: PropTypes.func.isRequired,
  evidence: PropTypes.shape({
    dataUrl: PropTypes.string,
    capturing: PropTypes.bool,
    error: PropTypes.string,
  }).isRequired,
  onCaptureEvidence: PropTypes.func.isRequired,
  onClearEvidence: PropTypes.func.isRequired,
  streaming: PropTypes.bool.isRequired,
  status: PropTypes.shape({ type: PropTypes.string, text: PropTypes.string }),
  evidenceStatus: PropTypes.shape({ type: PropTypes.string, text: PropTypes.string }),
  onObjection: PropTypes.func.isRequired,
};

export default ConsentStep;
