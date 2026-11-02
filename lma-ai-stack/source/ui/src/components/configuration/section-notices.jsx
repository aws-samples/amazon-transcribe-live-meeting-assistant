/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// What a section's schema cannot say, because it depends on how this
// deployment was built or on another section's saved value rather than on the
// section's own: shown at the top of the section it is about.

import React from 'react';
import PropTypes from 'prop-types';
import { Alert, KeyValuePairs, StatusIndicator } from '@cloudscape-design/components';

// The Virtual Participant's own fallback when nothing sets the mode.
const DEFAULT_ACTIVATION_MODE = 'wake_phrase';

const ACTIVATION_MODE_LABELS = {
  always_active: 'Always listening',
  wake_phrase: 'Wake phrase',
};

/** The voice assistant activation mode as saved, which is what the next Virtual Participant uses. */
const savedActivationMode = (configuration = {}) =>
  configuration?.virtual_participant?.voiceAssistantActivationMode || DEFAULT_ACTIVATION_MODE;

// The meeting modes are set in this section, and whether they apply depends on
// the Virtual Participant section's activation mode, as saved: an unsaved change
// to it is not what the next Virtual Participant would use.
const NovaSonicNotice = ({ configuration = {} }) => {
  const mode = savedActivationMode(configuration);
  if (mode === 'always_active') return null;
  return (
    <Alert type="info">
      The Group meeting and Translator meeting modes need an always-listening voice assistant. The Virtual Participant
      section sets the activation mode to <b>{ACTIVATION_MODE_LABELS[mode] || mode}</b>, so those modes are ignored
      until it is set to <b>Always listening</b> there, or switched for one meeting with a chat command.
    </Alert>
  );
};

NovaSonicNotice.propTypes = { configuration: PropTypes.shape({}) };

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

const SectionNotice = ({ section, settings = {}, configuration = {} }) => {
  const Notice = NOTICES[section];
  return Notice ? <Notice settings={settings} configuration={configuration} /> : null;
};

SectionNotice.propTypes = {
  section: PropTypes.string.isRequired,
  settings: SETTINGS,
  // Every section's values as saved, for a notice that depends on another section.
  configuration: PropTypes.shape({}),
};

export default SectionNotice;
