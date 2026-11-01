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

const AsrEngineNotice = ({ settings }) => {
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

AsrEngineNotice.propTypes = { settings: PropTypes.shape({}) };

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
  settings: PropTypes.shape({}),
};

export default SectionNotice;
