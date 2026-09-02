/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { generateClient } from 'aws-amplify/api';
import { fetchAuthSession } from 'aws-amplify/auth';
import React, { useState, useEffect, useCallback } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  FormField,
  Input,
  Checkbox,
  Button,
  Alert,
  Spinner,
  ColumnLayout,
  Box,
  ExpandableSection,
  StatusIndicator,
  KeyValuePairs,
  FileUpload,
  Badge,
} from '@cloudscape-design/components';

import useSettingsContext from '../../contexts/settings';

const client = generateClient();
const CONFIG_ID = 'CustomAsrConfig';

export const getAsrConfigQuery = `
  query GetAsrConfig($AsrConfigId: ID!) {
    getAsrConfig(AsrConfigId: $AsrConfigId) {
      AsrConfigId
      speakerThreshold
      minSegmentMs
      maxSpeakers
      endpointingMs
      requireCorroboration
      splitOnSpeakerChange
      liveTurnCut
      turnCutIntervalMs
      maxOpenSegmentMs
      diarizeByDefault
      engineDefaultMicrovm
    }
  }
`;

const updateAsrConfigMutation = `
  mutation UpdateAsrConfig($input: UpdateAsrConfigInput!) {
    updateAsrConfig(input: $input) {
      AsrConfigId
      Success
    }
  }
`;

// Ranges mirror the resolver's validation, so a bad value is caught before the
// round trip rather than being silently dropped server-side.
export const NUMERIC_LIMITS = {
  speakerThreshold: { min: 0, max: 1, label: 'Speaker similarity threshold' },
  minSegmentMs: { min: 0, max: 5000, label: 'Minimum utterance for speaker ID (ms)' },
  maxSpeakers: { min: 0, max: 30, label: 'Maximum speakers per channel' },
  endpointingMs: { min: 200, max: 5000, label: 'Endpointing silence (ms)' },
  turnCutIntervalMs: { min: 200, max: 10000, label: 'Speaker-change check interval (ms)' },
  maxOpenSegmentMs: { min: 0, max: 60000, label: 'Maximum open row duration (ms)' },
};

export const EMPTY = {
  speakerThreshold: '',
  minSegmentMs: '',
  maxSpeakers: '',
  endpointingMs: '',
  turnCutIntervalMs: '',
  maxOpenSegmentMs: '',
  requireCorroboration: false,
  splitOnSpeakerChange: true,
  liveTurnCut: true,
  diarizeByDefault: true,
  engineDefaultMicrovm: false,
};

const AsrConfigPage = () => {
  const { settings } = useSettingsContext();
  const engineDeployed = `${settings?.AsrEngineAvailable}` === 'true';
  // Separate question: the image may be transcription-only, in which case the engine
  // is deployed and configurable but produces no speaker labels.
  const diarizationAvailable = `${settings?.AsrDiarizationAvailable}` === 'true';
  const calibrateEndpoint = settings?.AsrCalibrateEndpoint || '';
  const speakerModelMeasured = `${settings?.AsrSpeakerModelMeasured}` !== 'false';
  const bundleId = settings?.AsrModelBundleId || '';
  const bundleThreshold = settings?.AsrBundleThreshold || '';

  const [config, setConfig] = useState(EMPTY);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState(null);
  const [calibrateFiles, setCalibrateFiles] = useState([]);
  const [calibrating, setCalibrating] = useState(false);
  const [calibration, setCalibration] = useState(null);
  const [calibrationError, setCalibrationError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await client.graphql({
        query: getAsrConfigQuery,
        variables: { AsrConfigId: CONFIG_ID },
      });
      const stored = result.data?.getAsrConfig || {};
      // Derived from EMPTY rather than listed again, so EMPTY is the single place a
      // field's default lives. Listing them twice meant a field added to EMPTY but
      // missed here loaded as `undefined`, which a controlled Input renders as the
      // literal string "undefined" in the box.
      setConfig(
        Object.fromEntries(Object.entries(EMPTY).map(([field, fallback]) => [field, stored[field] ?? fallback])),
      );
      setStatus(null);
    } catch (err) {
      setStatus({ type: 'error', text: `Could not load the ASR configuration: ${err.message || err}` });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (engineDeployed) {
      load();
    } else {
      setLoading(false);
    }
  }, [engineDeployed, load]);

  const invalidField = Object.entries(NUMERIC_LIMITS).find(([field, { min, max }]) => {
    const raw = `${config[field]}`.trim();
    if (raw === '') return false;
    const value = Number(raw);
    return Number.isNaN(value) || value < min || value > max;
  });

  const save = async () => {
    setSaving(true);
    try {
      await client.graphql({
        query: updateAsrConfigMutation,
        variables: { input: { AsrConfigId: CONFIG_ID, ConfigData: JSON.stringify(config) } },
      });
      setStatus({
        type: 'success',
        text: 'Saved. The next meeting to start picks this up — no redeploy needed.',
      });
    } catch (err) {
      setStatus({ type: 'error', text: `Could not save: ${err.message || err}` });
    } finally {
      setSaving(false);
    }
  };

  // Posted to the transcriber (where the launcher and the engine already are)
  // rather than AppSync, which has no route for a multi-megabyte body.
  const calibrate = async () => {
    setCalibrating(true);
    setCalibration(null);
    setCalibrationError(null);
    try {
      const session = await fetchAuthSession();
      const token = session?.tokens?.accessToken?.toString() || '';
      // Header, not the query string: this CloudFront distribution has access logging
      // enabled, so a token in the URL would be persisted to the log bucket (and the
      // server logs request.url at INFO). The WebSocket route has no choice — the
      // browser WebSocket API cannot set headers — but this is a plain fetch.
      const response = await fetch(calibrateEndpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'audio/wav', Authorization: `Bearer ${token}` },
        body: calibrateFiles[0],
      });
      const raw = await response.text();
      let body = null;
      try {
        body = JSON.parse(raw);
      } catch {
        body = null;
      }
      if (!response.ok || !body) {
        setCalibrationError(body?.message || `Calibration failed (HTTP ${response.status}). ${raw.slice(0, 300)}`);
        return;
      }
      setCalibration(body);
    } catch (err) {
      setCalibrationError(
        `Could not reach the calibration service: ${err.message || err}. It runs on the ` +
          'transcriber task, so this fails if no transcriber is running — check the ' +
          'TranscriberWebsocket log group for a line starting [ASR CALIBRATE].',
      );
    } finally {
      setCalibrating(false);
    }
  };

  const applyCalibration = () => {
    const measured = calibration?.result || {};
    setConfig({
      ...config,
      speakerThreshold: measured.speakerThreshold ?? config.speakerThreshold,
      minSegmentMs: measured.minSegmentMs ?? config.minSegmentMs,
    });
    setStatus({
      type: 'info',
      text: 'Measured values filled in above. Review them, then choose Save to apply.',
    });
  };

  const CONFIDENCE = {
    good: { type: 'success', text: 'Clear separation' },
    weak: { type: 'warning', text: 'Narrow separation' },
    unusable: { type: 'error', text: 'No usable threshold' },
  };

  if (!engineDeployed) {
    return (
      <Container
        header={
          <Header variant="h1" info={<Badge color="severity-medium">Experimental</Badge>}>
            ASR Configuration
          </Header>
        }
      >
        <Alert type="info" header="On-demand ASR engine is not deployed">
          These settings apply to the on-demand ASR &amp; diarization engine, which is{' '}
          <b>experimental and not production ready</b> — Amazon Transcribe remains the recommended engine. To evaluate
          it, set <b>TranscriptionEngine</b> to <b>MicrovmAsr</b> on the main stack.
        </Alert>
      </Container>
    );
  }

  const numberField = (field, description) => (
    <FormField
      label={NUMERIC_LIMITS[field].label}
      description={description}
      errorText={
        invalidField?.[0] === field
          ? `Must be between ${NUMERIC_LIMITS[field].min} and ${NUMERIC_LIMITS[field].max}`
          : undefined
      }
    >
      <Input
        value={`${config[field]}`}
        placeholder="deployment default"
        onChange={({ detail }) => setConfig({ ...config, [field]: detail.value })}
        disabled={saving}
      />
    </FormField>
  );

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            info={<Badge color="severity-medium">Experimental</Badge>}
            description={
              'Runtime settings for the on-demand ASR & speaker diarization engine. Every field is ' +
              'optional: leave it blank to use the deployment default. Changes take effect on ' +
              'the next meeting that starts — no stack update and no image rebuild.'
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={load} disabled={loading || saving}>
                  Reload
                </Button>
                <Button variant="primary" onClick={save} loading={saving} disabled={!!invalidField}>
                  Save
                </Button>
              </SpaceBetween>
            }
          >
            ASR Configuration
          </Header>
        }
      >
        {loading ? (
          <Spinner />
        ) : (
          <SpaceBetween size="l">
            <Alert type="warning" header="Experimental — not production ready">
              The on-demand ASR &amp; diarization engine is still under development. Transcript quality is below Amazon
              Transcribe&apos;s, speaker labels depend on a calibrated operating point, and defaults may change between
              releases. Amazon Transcribe remains the recommended engine for production meetings.
            </Alert>

            {!diarizationAvailable && (
              <Alert type="info" header="This deployment transcribes but does not diarize">
                The deployed bundle carries no speaker-embedding model, so the speaker settings below have no effect and
                calibration is not offered. Choose a bundle with an embedder to enable speaker labels.
              </Alert>
            )}

            {status && (
              <Alert type={status.type} dismissible onDismiss={() => setStatus(null)}>
                {status.text}
              </Alert>
            )}

            {diarizationAvailable && !speakerModelMeasured && (
              <Alert type="warning" header="This speaker model has no measured operating point">
                Speaker labels are being withheld: nobody has measured what cosine similarity means for this embedder,
                and a guessed threshold splits one person into several or merges several into one. Calibrate below, or
                set <b>Speaker similarity threshold</b> yourself, and diarization starts on the next meeting.
              </Alert>
            )}

            <Alert type="info" header="Every field here is an optional override">
              Leave a field blank and the value calibrated for the deployed model bundle is used.
              {bundleId && (
                <>
                  {' '}
                  This deployment runs <b>{bundleId}</b>
                  {bundleThreshold
                    ? `, calibrated at a similarity threshold of ${bundleThreshold}.`
                    : ', which has no calibrated threshold — hence the warning above.'}
                </>
              )}{' '}
              The threshold is specific to the <i>pairing</i>, not to the embedder alone: utterance length moves it as
              much as the model does (one embedder measured 0.30 on 1–2 s utterances and 0.68 on 5–20 s ones). So a
              number copied from another bundle will fragment one person into several or merge several into one.
            </Alert>

            <ColumnLayout columns={2}>
              {numberField(
                'speakerThreshold',
                'Cosine similarity above which two utterances are the same speaker. Lower merges ' +
                  "more eagerly. Blank uses the deployed bundle's calibrated value.",
              )}
              {numberField(
                'minSegmentMs',
                'Utterances shorter than this inherit the current speaker instead of being ' +
                  'embedded. Short clips are where phantom speakers come from. Default: 2500.',
              )}
              {numberField(
                'maxSpeakers',
                'A hard cap per audio channel; 0 discovers as many as appear. A cap bounds the ' +
                  'symptom rather than fixing the operating point, and a client that knows its own ' +
                  'meeting size can send a value that wins over this.',
              )}
              {numberField(
                'endpointingMs',
                'Trailing silence that closes an utterance. With speaker-change splitting on, ' +
                  'this is only a backstop rather than the thing that separates speakers. ' +
                  'Default: 1200.',
              )}
              {numberField(
                'turnCutIntervalMs',
                'How often to look for a speaker change inside an open utterance. Each search is ' +
                  'one segmentation-model window, so lowering this costs CPU on the MicroVM. ' +
                  'Default: 1000.',
              )}
              {numberField(
                'maxOpenSegmentMs',
                'Close a row after this much unbroken speech even when no speaker change is ' +
                  'found, so a monologue does not sit in the live transcript as one unlabelled ' +
                  "block. 0 follows the engine's own utterance boundaries. Default: 20000.",
              )}
            </ColumnLayout>

            <FormField
              label="Diarize by default"
              description="Whether meetings on this engine request speaker labels when the client does not say."
            >
              <Checkbox
                checked={config.diarizeByDefault}
                onChange={({ detail }) => setConfig({ ...config, diarizeByDefault: detail.checked })}
                disabled={saving}
              >
                Request speaker labels by default
              </Checkbox>
            </FormField>

            <ExpandableSection headerText="Advanced">
              <SpaceBetween size="l">
                <FormField
                  label="Split a row when the speaker changes mid-utterance"
                  description={
                    'Endpointing closes an utterance on silence, so two people speaking without a ' +
                    'gap share one row and one label. With the pyannote segmentation model baked ' +
                    'in, the engine finds the turn inside that utterance and emits a row per ' +
                    'speaker, cut at a word boundary. Has no effect if the image was built with ' +
                    'a bundle without a segmentation model.'
                  }
                >
                  <Checkbox
                    checked={config.splitOnSpeakerChange}
                    onChange={({ detail }) => setConfig({ ...config, splitOnSpeakerChange: detail.checked })}
                    disabled={saving}
                  >
                    Split rows on a speaker change
                  </Checkbox>
                </FormField>

                <FormField
                  label="Split as soon as the speaker changes, not when the pause comes"
                  description={
                    'The setting above splits an utterance retroactively, once it ends. This one ' +
                    'closes a row the moment a speaker change is confirmed, so a pause is no ' +
                    'longer what separates people - two speakers talking over each other get a ' +
                    'row each while they are still talking. Costs about one segmentation-model ' +
                    'window per second per channel on the MicroVM.'
                  }
                >
                  <Checkbox
                    checked={config.liveTurnCut}
                    onChange={({ detail }) => setConfig({ ...config, liveTurnCut: detail.checked })}
                    disabled={saving}
                  >
                    Close a row on a confirmed speaker change
                  </Checkbox>
                </FormField>

                <FormField
                  label="Use this engine for every streaming meeting"
                  description={
                    'Off by default, so every meeting uses Amazon Transcribe. Both engines can ' +
                    'produce speaker labels now, so asking for them does NOT pick an engine — ' +
                    'this switch is the only way a Stream Audio or Desktop Capture meeting reaches ' +
                    'the on-demand engine, and it routes ALL of them here. That also means no ' +
                    'content redaction, custom vocabulary, custom language model or language ' +
                    'identification for any of them. A meeting whose MicroVM cannot start still ' +
                    'falls back to Amazon Transcribe on its own.'
                  }
                >
                  <Checkbox
                    checked={config.engineDefaultMicrovm}
                    onChange={({ detail }) => setConfig({ ...config, engineDefaultMicrovm: detail.checked })}
                    disabled={saving}
                  >
                    Make the on-demand ASR engine the default
                  </Checkbox>
                </FormField>

                <FormField
                  label="Require corroboration before creating a speaker"
                  description={
                    'Withholds the first embedding that matches nobody until a second one agrees ' +
                    'with it. Measured: this rescues a threshold that is set too high, but with a ' +
                    'correct threshold it lowers attribution accuracy, and at a high threshold it ' +
                    'merged two different speakers. Use only if phantom speakers persist.'
                  }
                >
                  <Checkbox
                    checked={config.requireCorroboration}
                    onChange={({ detail }) => setConfig({ ...config, requireCorroboration: detail.checked })}
                    disabled={saving}
                  >
                    Require a second agreeing utterance
                  </Checkbox>
                </FormField>
              </SpaceBetween>
            </ExpandableSection>

            <Box variant="small">
              Speaker labels are per meeting and per audio channel; they are not identities. Accuracy is lowest in the
              first minute, while the model is still learning each voice.
            </Box>
          </SpaceBetween>
        )}
      </Container>

      {calibrateEndpoint && diarizationAvailable && (
        <Container
          header={
            <Header
              variant="h2"
              description={
                "Measure this deployment's speaker threshold from a two-channel recording instead " +
                'of inheriting a number measured on another model. One speaker per channel is the ' +
                'ground truth, so the sample can come from a rehearsed recording, a meeting you ' +
                'downloaded, or a public corpus.'
              }
            >
              Calibrate from a two-channel recording
            </Header>
          }
        >
          <SpaceBetween size="l">
            <ExpandableSection headerText="How to make a calibration file" defaultExpanded={!calibration}>
              <SpaceBetween size="s">
                <Box variant="p">
                  The file must be a <b>WAV, 16-bit PCM, with two channels</b>, where{' '}
                  <b>each channel carries one speaker</b>. That channel separation is the ground truth: pairs within a
                  channel are the same person, pairs across channels are definitely different people. Two to five
                  minutes is plenty, both speakers should talk several times, and they should avoid talking over each
                  other. Any sample rate works (it is resampled to 16 kHz); only the first 20 minutes are read.
                </Box>
                <Box variant="p">
                  <b>Easiest, on your own:</b> play a recording of someone else through your laptop speakers while you
                  talk into the microphone, captured with <b>Stream Audio</b> — its two channels are exactly system
                  audio and microphone. Alternate: let the recording talk for ~20s, then you talk for ~20s, four or five
                  times each. End the meeting, download the WAV from the meeting, and upload it here.
                </Box>
                <Box variant="p">
                  <b>From a public corpus:</b> any dataset with one file per speaker works — merge two speakers into the
                  two channels:
                </Box>
                <Box variant="code">
                  ffmpeg -i speakerA.wav -i speakerB.wav -filter_complex &quot;[0:a][1:a]amerge=inputs=2&quot; -ac 2 -ar
                  16000 -sample_fmt s16 calib.wav
                </Box>
                <Box variant="small">
                  The audio is embedded in memory and discarded — nothing is written to S3 or the transcript. A mono
                  file, or a stereo file with both voices in both channels, is refused rather than measured, because it
                  carries no ground truth.
                </Box>
              </SpaceBetween>
            </ExpandableSection>

            <FormField
              label="Calibration audio"
              description="Two-channel 16-bit PCM WAV, one speaker per channel."
              constraintText="Takes a few minutes: a MicroVM starts, the audio is embedded, then the VM is released."
            >
              <FileUpload
                onChange={({ detail }) => {
                  setCalibrateFiles(detail.value);
                  setCalibration(null);
                  setCalibrationError(null);
                }}
                value={calibrateFiles}
                accept="audio/wav,.wav"
                constraintText="WAV only, up to 64 MB"
                i18nStrings={{
                  uploadButtonText: () => 'Choose file',
                  dropzoneText: () => 'Drop a WAV file here',
                  removeFileAriaLabel: (index) => `Remove file ${index + 1}`,
                  limitShowFewer: 'Show fewer',
                  limitShowMore: 'Show more',
                  errorIconAriaLabel: 'Error',
                }}
                showFileSize
                showFileLastModified
                tokenLimit={1}
                disabled={calibrating}
              />
            </FormField>

            <Button onClick={calibrate} loading={calibrating} disabled={calibrating || calibrateFiles.length === 0}>
              {calibrating ? 'Measuring…' : 'Calibrate'}
            </Button>

            {calibrationError && (
              <Alert
                type="error"
                header="Calibration did not run"
                dismissible
                onDismiss={() => setCalibrationError(null)}
              >
                {calibrationError}
              </Alert>
            )}

            {calibration && (
              <SpaceBetween size="m">
                <KeyValuePairs
                  columns={4}
                  items={[
                    {
                      label: 'Result',
                      value: (
                        <StatusIndicator type={CONFIDENCE[calibration.result.confidence]?.type || 'info'}>
                          {CONFIDENCE[calibration.result.confidence]?.text || calibration.result.confidence}
                        </StatusIndicator>
                      ),
                    },
                    {
                      label: 'Measured threshold',
                      value: calibration.result.speakerThreshold ?? '—',
                    },
                    {
                      label: 'Minimum utterance',
                      value: calibration.result.minSegmentMs ? `${calibration.result.minSegmentMs} ms` : 'unchanged',
                    },
                    {
                      label: 'Separation',
                      value: Number.isFinite(calibration.result.separation)
                        ? calibration.result.separation.toFixed(3)
                        : '—',
                    },
                    {
                      label: 'Same speaker (p5 / median)',
                      value: `${(calibration.result.sameSpeakerP5 ?? 0).toFixed(3)} / ${(
                        calibration.result.sameSpeakerMedian ?? 0
                      ).toFixed(3)}`,
                    },
                    {
                      label: 'Different speakers (median / p95 / max)',
                      value: `${(calibration.result.differentSpeakerMedian ?? 0).toFixed(3)} / ${(
                        calibration.result.differentSpeakerP95 ?? 0
                      ).toFixed(3)} / ${(calibration.result.differentSpeakerMax ?? 0).toFixed(3)}`,
                    },
                    {
                      label: 'Utterances embedded',
                      value: `${calibration.segmentsEmbedded} of ${calibration.segmentsFound}`,
                    },
                    {
                      label: 'Audio analysed',
                      value: `${calibration.audioSecondsAnalysed}s at ${calibration.sourceSampleRate} Hz`,
                    },
                  ]}
                />

                {calibration.result.notes?.length > 0 && (
                  <Alert
                    type={calibration.result.confidence === 'unusable' ? 'warning' : 'info'}
                    header="What the measurement means"
                  >
                    <ul>
                      {calibration.result.notes.map((note) => (
                        <li key={note}>{note}</li>
                      ))}
                    </ul>
                  </Alert>
                )}

                <Button
                  variant="primary"
                  onClick={applyCalibration}
                  disabled={calibration.result.speakerThreshold === undefined}
                >
                  Use these values
                </Button>
                <Box variant="small">
                  Nothing has changed yet. Choosing this fills the fields above, and Save applies them to the next
                  meeting that starts.
                </Box>
              </SpaceBetween>
            )}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
};

export default AsrConfigPage;
