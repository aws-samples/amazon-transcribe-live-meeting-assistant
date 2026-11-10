/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Says, above the meeting's tabs, that transcription is paused until consent
 * is recorded: for a live meeting whose consent is enforced (notify or
 * opt-in) and does not allow capture, once the meeting has run for
 * CONSENT_PAUSE_GRACE_MS (use-consent-pause.js). It links to the consent
 * record on the Details tab, where the notice, the replies and the evidence
 * are. Dismissing it hides it until the consent status changes.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { Alert, Button } from '@cloudscape-design/components';

import { MODE_LABELS } from '../common/meeting-consent';
import { consentSummary } from '../common/meeting-context';
import useConsentPause from '../../hooks/use-consent-pause';

const ConsentPauseAlert = ({ isLive, consentState = null, startedAt = undefined, onShowRecord = undefined }) => {
  const consent = consentState?.consent || null;
  const summary = consentSummary(consentState);
  const due = useConsentPause({ isLive, summary, startedAt, consentStartedAt: consent?.StartedAt });
  const [dismissed, setDismissed] = useState(false);
  useEffect(() => setDismissed(false), [consent?.Status]);

  if (!due || dismissed) return null;

  const mode = MODE_LABELS[consent?.Mode] || summary.modeLabel;
  const objected = consent?.Status === 'objected';
  return (
    <Alert
      type="warning"
      dismissible
      i18nStrings={{ dismissAriaLabel: 'Dismiss' }}
      onDismiss={() => setDismissed(true)}
      header={
        objected
          ? 'Transcription is paused: a participant objected'
          : 'Transcription is paused until consent is recorded'
      }
      action={onShowRecord && <Button onClick={onShowRecord}>Show the consent record</Button>}
    >
      {objected
        ? `This meeting's consent setting is ${mode}. Nothing more is recorded or transcribed until every ` +
          'participant who objected consents.'
        : `This meeting's consent setting is ${mode}, and consent has not been recorded yet. Nothing is recorded ` +
          'or transcribed until it is: a reply to the notice in the meeting chat or out loud, or, where the ' +
          "meeting's rule allows, the owner's attestation."}
    </Alert>
  );
};

ConsentPauseAlert.propTypes = {
  isLive: PropTypes.bool.isRequired,
  // What useMeetingConsent returns.
  // eslint-disable-next-line react/forbid-prop-types
  consentState: PropTypes.object,
  // When the meeting started (ISO 8601).
  startedAt: PropTypes.string,
  onShowRecord: PropTypes.func,
};

export default ConsentPauseAlert;
