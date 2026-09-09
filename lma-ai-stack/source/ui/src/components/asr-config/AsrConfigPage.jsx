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
  RadioGroup,
  Button,
  Alert,
  Spinner,
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
      streamingEngineMicrovm
      virtualParticipantEngineMicrovm
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

// The engine's whole runtime surface: which engine each kind of meeting uses, and
// whether Virtual Participants ask for per-voice labels. The diarization operating
// point is the model bundle's, baked into the ASR image, and is not configurable.
export const EMPTY = {
  streamingEngineMicrovm: false,
  virtualParticipantEngineMicrovm: false,
  diarizeVirtualParticipant: false,
};

const ENGINE_ITEMS = [
  {
    value: 'transcribe',
    label: 'Amazon Transcribe',
    description: 'Recommended for production. Redaction, custom vocabulary and 30+ languages.',
  },
  {
    value: 'microvm',
    label: 'On-demand speech engine (experimental)',
    description:
      'Open-source models on a MicroVM launched per meeting in this account. Identifies separate speakers ' +
      'on one channel. English only, and transcript quality is below Amazon Transcribe. Falls back to ' +
      'Amazon Transcribe if it cannot start.',
  },
];

const AsrConfigPage = () => {
  const { settings } = useSettingsContext();
  const engineDeployed = `${settings?.AsrEngineAvailable}` === 'true';
  // A transcription-only bundle deploys the engine without speaker labels.
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
      // Derived from EMPTY so it stays the single place a field's default lives.
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
      setStatus({ type: 'success', text: 'Saved. Applies to the next meeting that starts.' });
    } catch (err) {
      setStatus({ type: 'error', text: `Could not save: ${err.message || err}` });
    } finally {
      setSaving(false);
    }
  };

  const engineRadio = (field) => (
    <RadioGroup
      value={config[field] ? 'microvm' : 'transcribe'}
      onChange={({ detail }) => setConfig({ ...config, [field]: detail.value === 'microvm' })}
      items={ENGINE_ITEMS}
      readOnly={saving}
    />
  );

  if (!engineDeployed) {
    return (
      <Container
        header={
          <Header variant="h1" info={<Badge color="severity-medium">Experimental</Badge>}>
            ASR Configuration
          </Header>
        }
      >
        <Alert type="info" header="The on-demand speech engine is not deployed">
          To evaluate the experimental on-demand speech engine, set <b>TranscriptionEngine</b> to <b>MicrovmAsr</b> on
          the main stack. Amazon Transcribe remains the recommended engine.
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
            'Choose which engine transcribes each kind of meeting. ' +
            'Changes apply to meetings started after you save.'
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
            Transcript quality on the on-demand engine is below Amazon Transcribe&apos;s and defaults may change between
            releases.
          </Alert>

          {status && (
            <Alert type={status.type} dismissible onDismiss={() => setStatus(null)}>
              {status.text}
            </Alert>
          )}

          <KeyValuePairs
            columns={2}
            items={[
              { label: 'Model bundle', value: bundleId || '—' },
              {
                label: 'Speaker identification',
                value: diarizationAvailable ? (
                  <StatusIndicator type="success">Available</StatusIndicator>
                ) : (
                  <StatusIndicator type="stopped">Not in this bundle</StatusIndicator>
                ),
              },
            ]}
          />

          <FormField
            label="Stream Audio, Chrome extension and Desktop Capture"
            description={
              'Speaker identification per channel is chosen per meeting, on the Stream Audio form or in the ' +
              "desktop app's settings."
            }
          >
            {engineRadio('streamingEngineMicrovm')}
          </FormField>

          <FormField
            label="Virtual Participants"
            description="Speaker names come from the meeting roster with either engine."
          >
            {engineRadio('virtualParticipantEngineMicrovm')}
          </FormField>

          <FormField
            label="Virtual Participant voice separation"
            description={
              'When one attendee is several people, such as a conference room, label their voices ' +
              '"Name (spk_0)", "Name (spk_1)". Requires the on-demand engine for Virtual Participants.'
            }
          >
            <Checkbox
              checked={config.diarizeVirtualParticipant}
              onChange={({ detail }) => setConfig({ ...config, diarizeVirtualParticipant: detail.checked })}
              disabled={saving || !diarizationAvailable || !config.virtualParticipantEngineMicrovm}
            >
              Identify separate voices behind one attendee
            </Checkbox>
          </FormField>
        </SpaceBetween>
      )}
    </Container>
  );
};

export default AsrConfigPage;
