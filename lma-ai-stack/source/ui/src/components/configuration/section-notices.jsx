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
import { Alert, Box, KeyValuePairs, StatusIndicator } from '@cloudscape-design/components';

import awsExports from '../../aws-exports';
import { GEOGRAPHY, GLOBAL, IN_REGION, describeScope, resolveForGeography } from './inference-geography';

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

/** Whether a meeting language is one the ASR MicroVM engine transcribes: English, as isMicrovmLanguage. */
const isMicrovmLanguage = (languageCode) =>
  !languageCode || languageCode === 'en' || `${languageCode}`.startsWith('en-');

/**
 * The Amazon Transcribe options a saved transcription section uses that the
 * ASR MicroVM engine does not offer, as the page names them, each with whether
 * it also concerns Virtual Participants (which have no custom language model
 * setting). A meeting using any of them stays on Amazon Transcribe: the same rule
 * as transcribeOnlyOptions in the transcriber's and the Virtual Participant's
 * configuration-store.ts.
 */
export const transcribeOnlyOptionsInUse = (transcription = {}) => {
  const options = [];
  if (transcription?.contentRedactionEnabled === true) options.push({ name: 'content redaction', vp: true });
  if (transcription?.customVocabularyName) options.push({ name: 'a custom vocabulary', vp: true });
  if (transcription?.customLanguageModelName) options.push({ name: 'a custom language model', vp: false });
  const languageCode = transcription?.languageCode;
  if (['identify-language', 'identify-multiple-languages'].includes(languageCode)) {
    options.push({ name: 'language identification', vp: true });
  } else if (!isMicrovmLanguage(languageCode)) {
    options.push({ name: `the language ${languageCode}`, vp: true });
  }
  return options;
};

/** The names alone; see transcribeOnlyOptionsInUse. */
export const transcribeOnlyOptions = (transcription = {}) =>
  transcribeOnlyOptionsInUse(transcription).map((option) => option.name);

// Without the ASR MicroVM engine every meeting is transcribed by Amazon
// Transcribe, so the section shows only the voice separation switch, which
// that engine honours too, and says where the engine choices went.
const AsrEngineNotice = ({ settings = {}, configuration = {} }) => {
  if (`${settings?.AsrEngineAvailable}` !== 'true') {
    return (
      <Alert type="info" header="Every meeting is transcribed by Amazon Transcribe">
        The ASR MicroVM speech engine is not deployed, so the choice of engine for each kind of meeting is not shown.
        Virtual Participant voice separation works on Amazon Transcribe. The ASR MicroVM engine is deployed when
        EnableMicrovmAsr is true on the main stack (the default) in a Region with AWS Lambda MicroVMs.
      </Alert>
    );
  }
  const diarization = `${settings?.AsrDiarizationAvailable}` === 'true';
  const inUse = transcribeOnlyOptionsInUse(configuration?.transcription);
  const affected = inUse.some((option) => option.vp)
    ? 'every streaming meeting and Virtual Participant is'
    : 'every streaming meeting is';
  return (
    <>
      <Alert type="info" header="The ASR MicroVM engine is the default">
        The ASR MicroVM engine transcribes English only, and does not offer Amazon Transcribe&apos;s content redaction,
        custom vocabulary, custom language model or language identification. While the Amazon Transcribe section sets
        another language or uses any of those, meetings stay on Amazon Transcribe whichever engine is selected here. A
        meeting whose MicroVM cannot start also falls back to Amazon Transcribe.
      </Alert>
      {inUse.length > 0 && (
        <Alert type="warning" header="These settings keep meetings on Amazon Transcribe">
          The Amazon Transcribe section uses {inUse.map((option) => option.name).join(', ')}, so {affected} transcribed
          by Amazon Transcribe, even where the ASR MicroVM engine is selected below.
        </Alert>
      )}
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

AsrEngineNotice.propTypes = { settings: SETTINGS, configuration: PropTypes.shape({}) };

// The model settings the Model inference geography applies to, as the page
// labels them. The consent evidence check model is the Meeting Consent
// section's, and is shown from that section as saved.
const MODEL_FEATURES = [
  ['summaryModelId', 'Summaries and meeting insights'],
  ['meetingAssistModelId', 'Meeting assistant'],
  ['knowledgeBaseModelId', 'Meeting assistant knowledge bases'],
  ['keyframeTextModelId', 'Key frame text'],
];

const GEOGRAPHY_LABELS = {
  [GLOBAL]: 'Global',
  [GEOGRAPHY]: 'Same geography as this deployment',
  [IN_REGION]: 'In-region only',
};

/** Each feature's model and how it runs under the section's geography, as edited. */
export const modelPlacements = (values = {}, configuration = {}, region = '') => {
  const geography = values.inferenceGeography || GLOBAL;
  const features = MODEL_FEATURES.map(([setting, label]) => [label, values[setting]]);
  const verifier = configuration?.meeting_consent?.verifierModelId;
  if (verifier) features.push(['Consent evidence check (Meeting Consent section)', verifier]);
  return features
    .filter(([, modelId]) => modelId)
    .map(([label, modelId]) => ({
      label,
      chosen: modelId,
      geography,
      ...resolveForGeography(modelId, geography, region),
    }));
};

const Placement = ({ placement, region }) => {
  if (placement.unavailable) {
    return (
      <StatusIndicator type="error">
        {`${placement.chosen} has no Amazon Bedrock id in this geography; choose another model`}
      </StatusIndicator>
    );
  }
  const scope = describeScope(placement.modelId, region);
  return (
    <>
      <Box variant="code">{placement.modelId}</Box>
      {` runs in ${scope}`}
      {placement.modelId !== placement.chosen && ` (chosen: ${placement.chosen})`}
      {/* Under Global nothing is checked, as before the setting existed. Under
          the other geographies an id the shipped listing does not show for this
          Region is flagged whether or not it was changed: a plain id the Region
          does not offer is kept as chosen, and its call fails. */}
      {!placement.verified && placement.geography !== GLOBAL && (
        <>
          {' '}
          <StatusIndicator type="warning">
            not in this Region&apos;s Amazon Bedrock listing; the call may fail
          </StatusIndicator>
        </>
      )}
    </>
  );
};

Placement.propTypes = {
  placement: PropTypes.shape({
    chosen: PropTypes.string,
    geography: PropTypes.string,
    modelId: PropTypes.string,
    verified: PropTypes.bool,
    unavailable: PropTypes.bool,
  }).isRequired,
  region: PropTypes.string.isRequired,
};

// Where each model of the section runs inference, under the geography as edited,
// so the effect of a change is visible before it is saved.
const LanguageModelsNotice = ({ values = {}, configuration = {}, region = awsExports.aws_project_region || '' }) => {
  const geography = values.inferenceGeography || GLOBAL;
  const placements = modelPlacements(values, configuration, region);
  const unavailable = placements.filter((placement) => placement.unavailable);
  return (
    <>
      {unavailable.length > 0 && (
        <Alert type="warning" header="Some features have no model in the selected geography">
          {`Under ${GEOGRAPHY_LABELS[geography] || geography} in ${region}, ` +
            `${unavailable.map((placement) => placement.label).join(', ')} ` +
            `${unavailable.length === 1 ? 'reports an error' : 'report an error'} instead of calling Amazon Bedrock ` +
            'outside it. Choose a model offered in this geography, or another geography.'}
        </Alert>
      )}
      <KeyValuePairs
        columns={1}
        items={placements.map((placement) => ({
          label: placement.label,
          value: <Placement placement={placement} region={region} />,
        }))}
      />
    </>
  );
};

LanguageModelsNotice.propTypes = {
  values: PropTypes.shape({ inferenceGeography: PropTypes.string }),
  configuration: PropTypes.shape({}),
  region: PropTypes.string,
};

// Why a section a link asked for is not shown, where its schema's
// availableWhen does not hold in this deployment.
// No shipped section is withheld today; the asr_engine section hides only its
// engine choices instead (see AsrEngineNotice).
export const UNAVAILABLE_REASONS = {};

const NOTICES = {
  nova_sonic_config: NovaSonicNotice,
  asr_engine: AsrEngineNotice,
  language_models: LanguageModelsNotice,
};

const SectionNotice = ({ section, settings = {}, configuration = {}, values = {}, region = undefined }) => {
  const Notice = NOTICES[section];
  return Notice ? <Notice settings={settings} configuration={configuration} values={values} region={region} /> : null;
};

SectionNotice.propTypes = {
  section: PropTypes.string.isRequired,
  settings: SETTINGS,
  // Every section's values as saved, for a notice that depends on another section.
  configuration: PropTypes.shape({}),
  // This section's values as edited, for a notice that previews them.
  values: PropTypes.shape({}),
  // The deployment's Region; the build's when not given.
  region: PropTypes.string,
};

export default SectionNotice;
