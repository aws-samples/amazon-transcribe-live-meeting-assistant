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
} from '@cloudscape-design/components';

import useSettingsContext from '../../contexts/settings';

const client = generateClient();
const CONFIG_ID = 'CustomAsrConfig';

const getAsrConfigQuery = `
  query GetAsrConfig($AsrConfigId: ID!) {
    getAsrConfig(AsrConfigId: $AsrConfigId) {
      AsrConfigId
      speakerThreshold
      minSegmentMs
      maxSpeakers
      endpointingMs
      requireCorroboration
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
const NUMERIC_LIMITS = {
  speakerThreshold: { min: 0, max: 1, label: 'Speaker similarity threshold' },
  minSegmentMs: { min: 0, max: 5000, label: 'Minimum utterance for speaker ID (ms)' },
  maxSpeakers: { min: 0, max: 30, label: 'Maximum speakers per channel' },
  endpointingMs: { min: 200, max: 5000, label: 'Endpointing silence (ms)' },
};

const EMPTY = {
  speakerThreshold: '',
  minSegmentMs: '',
  maxSpeakers: '',
  endpointingMs: '',
  requireCorroboration: false,
  diarizeByDefault: true,
  engineDefaultMicrovm: false,
};

const AsrConfigPage = () => {
  const { settings } = useSettingsContext();
  const engineDeployed = `${settings?.AsrDiarizationAvailable}` === 'true';
  const calibrateEndpoint = settings?.AsrCalibrateEndpoint || '';
  const speakerModelMeasured = `${settings?.AsrSpeakerModelMeasured}` !== 'false';

  const [config, setConfig] = useState(EMPTY);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState(null);
  const [calibrateCallId, setCalibrateCallId] = useState('');
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
      setConfig({
        speakerThreshold: stored.speakerThreshold ?? '',
        minSegmentMs: stored.minSegmentMs ?? '',
        maxSpeakers: stored.maxSpeakers ?? '',
        endpointingMs: stored.endpointingMs ?? '',
        requireCorroboration: stored.requireCorroboration ?? false,
        diarizeByDefault: stored.diarizeByDefault ?? true,
        engineDefaultMicrovm: stored.engineDefaultMicrovm ?? false,
      });
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

  /**
   * Measure the operating point from a meeting this deployment already recorded.
   *
   * The transcriber task does the work (that is where the audio, the launcher and
   * the engine already are), so this posts to the WebSocket domain rather than
   * AppSync. Minutes, not seconds: a MicroVM has to start and embed the audio.
   */
  const calibrate = async () => {
    setCalibrating(true);
    setCalibration(null);
    setCalibrationError(null);
    try {
      const session = await fetchAuthSession();
      const token = session?.tokens?.accessToken?.toString() || '';
      // Token and parameters go on the query string: the WebSocket route
      // authenticates the same way, and a request with no custom headers and no
      // body content type needs no CORS preflight.
      const url =
        `${calibrateEndpoint}?authorization=${encodeURIComponent(`Bearer ${token}`)}` +
        `&callId=${encodeURIComponent(calibrateCallId.trim())}`;
      const response = await fetch(url, { method: 'POST' });
      const body = await response.json();
      if (!response.ok) {
        setCalibrationError(body?.message || `Calibration failed (HTTP ${response.status}).`);
        return;
      }
      setCalibration(body);
    } catch (err) {
      setCalibrationError(
        `Could not reach the calibration service: ${err.message || err}. It runs on the ` +
          'transcriber task, so this fails if no transcriber is running.',
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
      <Container header={<Header variant="h1">ASR Configuration</Header>}>
        <Alert type="info" header="On-demand ASR engine is not deployed">
          These settings apply to the on-demand ASR &amp; diarization engine. Deploy it by setting{' '}
          <b>TranscriptionEngine</b> to <b>MicrovmAsr</b> on the main stack.
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
            description={
              'Runtime settings for the on-demand ASR & speaker diarization engine. Every field is ' +
              'optional: leave it blank to use the CloudFormation parameter. Changes take effect on ' +
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
            {status && (
              <Alert type={status.type} dismissible onDismiss={() => setStatus(null)}>
                {status.text}
              </Alert>
            )}

            {!speakerModelMeasured && (
              <Alert type="warning" header="This speaker model has no measured operating point">
                Speaker labels are being withheld: nobody has measured what cosine similarity means for this embedder,
                and a guessed threshold splits one person into several or merges several into one. Calibrate below, or
                set <b>Speaker similarity threshold</b> yourself, and diarization starts on the next meeting.
              </Alert>
            )}

            <Alert type="info" header="The threshold belongs to the speaker model">
              These values were measured against the default TitaNet embedder, where two different speakers scored at
              most 0.107 and the same speaker 0.25–0.5. A different speaker model has a different scale — check{' '}
              <code>recommendedThreshold</code> in the ASR stack&apos;s
              <code> catalog.json</code> before changing models, or one person will fragment into several and several
              will merge into one.
            </Alert>

            <ColumnLayout columns={2}>
              {numberField(
                'speakerThreshold',
                'Cosine similarity above which two utterances are the same speaker. Lower merges ' +
                  'more eagerly. Measured default: 0.2.',
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
              {numberField('endpointingMs', 'Trailing silence that closes an utterance. Default: 1200.')}
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
                  label="Use this engine for every streaming meeting"
                  description={
                    'Off by default: meetings use Amazon Transcribe unless a client asks for ' +
                    'diarization. Turning this on routes ALL Stream Audio and Desktop Capture ' +
                    'meetings here, which also means no content redaction, custom vocabulary, ' +
                    'custom language model or language identification for any of them.'
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

      {calibrateEndpoint && (
        <Container
          header={
            <Header
              variant="h2"
              description={
                'Measure the threshold from one of your own recorded meetings instead of inheriting ' +
                'a number from another model. The two audio channels are the ground truth: utterances ' +
                'on the microphone are one person and utterances on the meeting audio are someone ' +
                'else, which is exactly the comparison a threshold has to get right.'
              }
            >
              Calibrate from a recorded meeting
            </Header>
          }
        >
          <SpaceBetween size="l">
            <FormField
              label="Meeting ID"
              description={
                'A meeting that was recorded, where both sides spoke and were not talking over each ' +
                'other. Copy the Meeting ID from the Meetings list.'
              }
              constraintText="Takes a few minutes: a MicroVM starts, the audio is embedded, then the VM is released."
            >
              <Input
                value={calibrateCallId}
                placeholder="Stream Audio - 2026-08-12-10:17:29.439"
                onChange={({ detail }) => setCalibrateCallId(detail.value)}
                disabled={calibrating}
              />
            </FormField>

            <Button onClick={calibrate} loading={calibrating} disabled={calibrating || calibrateCallId.trim() === ''}>
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
