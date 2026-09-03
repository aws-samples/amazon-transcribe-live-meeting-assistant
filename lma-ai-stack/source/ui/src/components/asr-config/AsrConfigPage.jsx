/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { generateClient } from 'aws-amplify/api';
import React, { useState, useEffect, useCallback } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  FormField,
  Checkbox,
  Button,
  Alert,
  Spinner,
  Box,
  KeyValuePairs,
  StatusIndicator,
  Badge,
} from '@cloudscape-design/components';

import useSettingsContext from '../../contexts/settings';

const client = generateClient();
const CONFIG_ID = 'CustomAsrConfig';

export const getAsrConfigQuery = `
  query GetAsrConfig($AsrConfigId: ID!) {
    getAsrConfig(AsrConfigId: $AsrConfigId) {
      AsrConfigId
      engineDefaultMicrovm
      diarizeVirtualParticipant
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

// The whole runtime surface of the engine: two switches. Everything that used to be
// tunable here (threshold, utterance floor, turn cutting, speaker cap) is now the
// model bundle's measured operating point, baked into the ASR image.
export const EMPTY = {
  engineDefaultMicrovm: false,
  diarizeVirtualParticipant: false,
};

const AsrConfigPage = () => {
  const { settings } = useSettingsContext();
  const engineDeployed = `${settings?.AsrEngineAvailable}` === 'true';
  // The image may be transcription-only, in which case the engine is deployed but
  // produces no speaker labels and the VP diarization switch cannot do anything.
  const diarizationAvailable = `${settings?.AsrDiarizationAvailable}` === 'true';
  const bundleId = settings?.AsrModelBundleId || '';

  const [config, setConfig] = useState(EMPTY);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await client.graphql({
        query: getAsrConfigQuery,
        variables: { AsrConfigId: CONFIG_ID },
      });
      const stored = result.data?.getAsrConfig || {};
      // Derived from EMPTY so it stays the single place a field's default lives; a
      // field listed twice once loaded as `undefined` and rendered as that string.
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

  return (
    <Container
      header={
        <Header
          variant="h1"
          info={<Badge color="severity-medium">Experimental</Badge>}
          description={
            'Where the on-demand ASR & speaker diarization engine is used. Changes take effect on ' +
            'the next meeting that starts — no stack update and no image rebuild.'
          }
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={load} disabled={loading || saving}>
                Reload
              </Button>
              <Button variant="primary" onClick={save} loading={saving}>
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
            Transcribe&apos;s and defaults may change between releases. Amazon Transcribe remains the recommended engine
            for production meetings.
          </Alert>

          {status && (
            <Alert type={status.type} dismissible onDismiss={() => setStatus(null)}>
              {status.text}
            </Alert>
          )}

          <KeyValuePairs
            columns={2}
            items={[
              { label: 'Deployed model bundle', value: bundleId || '—' },
              {
                label: 'Speaker labels',
                value: diarizationAvailable ? (
                  <StatusIndicator type="success">Available — operating point baked into the image</StatusIndicator>
                ) : (
                  <StatusIndicator type="stopped">Not in this bundle (transcription only)</StatusIndicator>
                ),
              },
            ]}
          />

          <FormField
            label="Default engine for streaming meetings and Virtual Participants"
            description={
              'Off (the default): every meeting uses Amazon Transcribe. On: Stream Audio, the Desktop Capture apps ' +
              'and Virtual Participants use the on-demand engine unless a meeting picks an engine itself in the ' +
              'Stream Audio form. Meetings on this engine get no content redaction, custom vocabulary, custom ' +
              'language model or language identification, and it is English only. A meeting whose MicroVM cannot ' +
              'start still falls back to Amazon Transcribe on its own.'
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
            label="Virtual Participant: tell apart several voices behind one attendee"
            description={
              'A Virtual Participant already names each speaker from the meeting roster, which is better than any ' +
              'voice-derived label — so this is off by default. Turn it on when one attendee tile carries several ' +
              'people (a conference room, or a shared screen playing a recording): the engine then labels the ' +
              'voices behind that name as "Name (spk_0)", "Name (spk_1)". Applies only to Virtual Participants ' +
              'transcribed by the on-demand engine.'
            }
          >
            <Checkbox
              checked={config.diarizeVirtualParticipant}
              onChange={({ detail }) => setConfig({ ...config, diarizeVirtualParticipant: detail.checked })}
              disabled={saving || !diarizationAvailable}
            >
              Identify separate voices behind each Virtual Participant attendee
            </Checkbox>
          </FormField>

          <Box variant="small">
            There is nothing to tune. The similarity threshold and minimum utterance length that decide when two
            utterances are the same person were measured for the deployed bundle&apos;s speaker model and are baked into
            the ASR image; a different bundle carries its own. Speaker labels are per meeting and per audio channel;
            they are not identities, and are least accurate in the first minute while the model is still learning each
            voice.
          </Box>
        </SpaceBetween>
      )}
    </Container>
  );
};

export default AsrConfigPage;
