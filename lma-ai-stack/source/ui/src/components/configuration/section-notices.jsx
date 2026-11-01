/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// What a section's schema cannot say, because it depends on how this
// deployment was built rather than on the stored configuration: shown at the
// top of the section it is about.

import React from 'react';
import PropTypes from 'prop-types';
import { Alert, KeyValuePairs, StatusIndicator } from '@cloudscape-design/components';

const VOICE_ASSISTANT_ACTIVATION_MODE = import.meta.env.VITE_VOICE_ASSISTANT_ACTIVATION_MODE || 'wake_phrase';

const NovaSonicNotice = () =>
  VOICE_ASSISTANT_ACTIVATION_MODE === 'always_active' ? null : (
    <Alert type="info">
      The Group meeting and Translator meeting modes take effect only when the Virtual Participant is deployed with its
      voice assistant activation mode set to <b>always_active</b>. This deployment uses{' '}
      <b>{VOICE_ASSISTANT_ACTIVATION_MODE}</b>, so those modes are ignored until the main stack is updated.
    </Alert>
  );

// Without the on-demand engine every meeting is transcribed by Amazon
// Transcribe, so the section shows only the voice separation switch, which
// that engine honours too, and says where the engine choices went.
const AsrEngineNotice = ({ settings = {} }) => {
  if (`${settings?.AsrEngineAvailable}` !== 'true') {
    return (
      <Alert type="info" header="Every meeting is transcribed by Amazon Transcribe">
        The on-demand speech engine is not deployed, so the choice of engine for each kind of meeting is not shown.
        Virtual Participant voice separation works on Amazon Transcribe. To evaluate the experimental on-demand engine,
        set EnableMicrovmAsr to true on the main stack; Amazon Transcribe remains the recommended engine.
      </Alert>
    );
  }
  const diarization = `${settings?.AsrDiarizationAvailable}` === 'true';
  return (
    <>
      <Alert type="warning" header="Experimental — not production ready">
        Transcript quality on the on-demand engine is below Amazon Transcribe&apos;s and its defaults may change between
        releases. It falls back to Amazon Transcribe if it cannot start.
      </Alert>
      <KeyValuePairs
        columns={2}
        items={[
          { label: 'Model bundle', value: settings?.AsrModelBundleName || settings?.AsrModelBundleId || '—' },
          {
            label: 'Speaker identification',
            value: diarization ? (
              <StatusIndicator type="success">Available</StatusIndicator>
            ) : (
              <StatusIndicator type="stopped">Not in this bundle</StatusIndicator>
            ),
          },
        ]}
      />
    </>
  );
};

const SETTINGS = PropTypes.shape({
  AsrEngineAvailable: PropTypes.oneOfType([PropTypes.string, PropTypes.bool]),
  AsrDiarizationAvailable: PropTypes.oneOfType([PropTypes.string, PropTypes.bool]),
  AsrModelBundleName: PropTypes.string,
  AsrModelBundleId: PropTypes.string,
});

AsrEngineNotice.propTypes = { settings: SETTINGS };

// Why a section a link asked for is not shown, where its schema's
// availableWhen does not hold in this deployment.
// No shipped section is withheld today; the asr_engine section hides only its
// engine choices instead (see AsrEngineNotice).
export const UNAVAILABLE_REASONS = {};

const NOTICES = {
  nova_sonic_config: NovaSonicNotice,
  asr_engine: AsrEngineNotice,
};

const SectionNotice = ({ section, settings = {} }) => {
  const Notice = NOTICES[section];
  return Notice ? <Notice settings={settings} /> : null;
};

SectionNotice.propTypes = {
  section: PropTypes.string.isRequired,
  settings: SETTINGS,
};

export default SectionNotice;
