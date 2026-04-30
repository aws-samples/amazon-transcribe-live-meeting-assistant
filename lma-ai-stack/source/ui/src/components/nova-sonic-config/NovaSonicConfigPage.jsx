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
  Input,
  Textarea,
  Select,
  Toggle,
  Button,
  Alert,
  Spinner,
  ColumnLayout,
  Box,
  ExpandableSection,
} from '@cloudscape-design/components';

const client = generateClient();
const PROMPT_MODE_OPTIONS = [
  { label: 'Base', value: 'base' },
  { label: 'Inject', value: 'inject' },
  { label: 'Replace', value: 'replace' },
];

const SENSITIVITY_OPTIONS = [
  { label: 'LOW', value: 'LOW' },
  { label: 'MEDIUM', value: 'MEDIUM' },
  { label: 'HIGH', value: 'HIGH' },
];

const getNovaSonicConfigQuery = `
  query GetNovaSonicConfig($NovaSonicConfigId: ID!) {
    getNovaSonicConfig(NovaSonicConfigId: $NovaSonicConfigId) {
      NovaSonicConfigId
    }
  }
`;

const updateNovaSonicConfigMutation = `
  mutation UpdateNovaSonicConfig($input: UpdateNovaSonicConfigInput!) {
    updateNovaSonicConfig(input: $input) {
      NovaSonicConfigId
      Success
    }
  }
`;

const NovaSonicConfigPage = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [defaultConfig, setDefaultConfig] = useState({});
  // eslint-disable-next-line no-unused-vars
  const [customConfig, setCustomConfig] = useState({});

  // Form state
  const [systemPrompt, setSystemPrompt] = useState('');
  const [promptMode, setPromptMode] = useState(null);
  const [modelId, setModelId] = useState('');
  const [voiceId, setVoiceId] = useState('');
  const [endpointingSensitivity, setEndpointingSensitivity] = useState(null);
  const [groupMeetingMode, setGroupMeetingMode] = useState(false);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [defaultResult, customResult] = await Promise.all([
        client.graphql({ query: getNovaSonicConfigQuery, variables: { NovaSonicConfigId: 'DefaultNovaSonicConfig' } }),
        client.graphql({ query: getNovaSonicConfigQuery, variables: { NovaSonicConfigId: 'CustomNovaSonicConfig' } }),
      ]);

      const defaultData = JSON.parse(defaultResult.data.getNovaSonicConfig.NovaSonicConfigId);
      const customData = JSON.parse(customResult.data.getNovaSonicConfig.NovaSonicConfigId);

      setDefaultConfig(defaultData || {});
      setCustomConfig(customData || {});

      // Populate form with custom values (if set), otherwise leave empty to show defaults
      if (customData) {
        setSystemPrompt(customData.systemPrompt || '');
        setPromptMode(
          customData.promptMode ? PROMPT_MODE_OPTIONS.find((o) => o.value === customData.promptMode) : null,
        );
        setModelId(customData.modelId || '');
        setVoiceId(customData.voiceId || '');
        setEndpointingSensitivity(
          customData.endpointingSensitivity
            ? SENSITIVITY_OPTIONS.find((o) => o.value === customData.endpointingSensitivity)
            : null,
        );
        setGroupMeetingMode(customData.groupMeetingMode === true);
      }
    } catch (err) {
      console.error('Error loading Nova Sonic config:', err);
      setError('Failed to load configuration. Please try again.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const configData = {};
      if (systemPrompt) configData.systemPrompt = systemPrompt;
      if (promptMode) configData.promptMode = promptMode.value;
      if (modelId) configData.modelId = modelId;
      if (voiceId) configData.voiceId = voiceId;
      if (endpointingSensitivity) configData.endpointingSensitivity = endpointingSensitivity.value;
      configData.groupMeetingMode = groupMeetingMode;

      await client.graphql({
        query: updateNovaSonicConfigMutation,
        variables: {
          input: {
            NovaSonicConfigId: 'CustomNovaSonicConfig',
            ConfigData: JSON.stringify(configData),
          },
        },
      });

      setSuccess('Configuration saved successfully.');
      await loadConfig();
    } catch (err) {
      console.error('Error saving Nova Sonic config:', err);
      setError('Failed to save configuration. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  const handleResetToDefaults = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await client.graphql({
        query: updateNovaSonicConfigMutation,
        variables: {
          input: {
            NovaSonicConfigId: 'CustomNovaSonicConfig',
            ConfigData: JSON.stringify({}),
          },
        },
      });

      setSystemPrompt('');
      setPromptMode(null);
      setModelId('');
      setVoiceId('');
      setEndpointingSensitivity(null);
      setGroupMeetingMode(false);
      setSuccess('Custom overrides cleared. Default configuration will be used.');
      await loadConfig();
    } catch (err) {
      console.error('Error resetting config:', err);
      setError('Failed to reset configuration. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Container header={<Header variant="h1">Nova Sonic Configuration</Header>}>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" /> Loading configuration...
        </Box>
      </Container>
    );
  }

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description={
              'Configure the Nova Sonic voice assistant. Custom values override defaults ' +
              'and are preserved during stack updates.'
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={handleResetToDefaults} loading={saving}>
                  Reset to Defaults
                </Button>
                <Button variant="primary" onClick={handleSave} loading={saving}>
                  Save Changes
                </Button>
              </SpaceBetween>
            }
          >
            Nova Sonic Configuration
          </Header>
        }
      >
        <SpaceBetween size="l">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}
          {success && (
            <Alert type="success" dismissible onDismiss={() => setSuccess(null)}>
              {success}
            </Alert>
          )}

          <FormField
            label="System Prompt"
            description={`Custom override. Default: "${defaultConfig.systemPrompt || 'N/A'}"`}
          >
            <Textarea
              value={systemPrompt}
              onChange={({ detail }) => setSystemPrompt(detail.value)}
              placeholder={defaultConfig.systemPrompt || 'Enter system prompt...'}
              rows={4}
            />
          </FormField>

          <ColumnLayout columns={2}>
            <FormField label="Prompt Mode" description={`Default: ${defaultConfig.promptMode || 'N/A'}`}>
              <Select
                selectedOption={promptMode}
                onChange={({ detail }) => setPromptMode(detail.selectedOption)}
                options={PROMPT_MODE_OPTIONS}
                placeholder="Use default"
              />
            </FormField>

            <FormField label="Model ID" description={`Default: ${defaultConfig.modelId || 'N/A'}`}>
              <Input
                value={modelId}
                onChange={({ detail }) => setModelId(detail.value)}
                placeholder={defaultConfig.modelId || 'Enter model ID...'}
              />
            </FormField>

            <FormField label="Voice ID" description={`Default: ${defaultConfig.voiceId || 'N/A'}`}>
              <Input
                value={voiceId}
                onChange={({ detail }) => setVoiceId(detail.value)}
                placeholder={defaultConfig.voiceId || 'Enter voice ID...'}
              />
            </FormField>

            <FormField
              label="Endpointing Sensitivity"
              description={`Default: ${defaultConfig.endpointingSensitivity || 'N/A'}`}
            >
              <Select
                selectedOption={endpointingSensitivity}
                onChange={({ detail }) => setEndpointingSensitivity(detail.selectedOption)}
                options={SENSITIVITY_OPTIONS}
                placeholder="Use default"
              />
            </FormField>
          </ColumnLayout>

          <FormField
            label="Group Meeting Mode"
            description={`Default: ${
              defaultConfig.groupMeetingMode !== undefined ? String(defaultConfig.groupMeetingMode) : 'N/A'
            }`}
          >
            <Toggle checked={groupMeetingMode} onChange={({ detail }) => setGroupMeetingMode(detail.checked)}>
              {groupMeetingMode ? 'Enabled' : 'Disabled'}
            </Toggle>
          </FormField>
        </SpaceBetween>
      </Container>

      <ExpandableSection headerText="View Default Configuration (read-only)" variant="container">
        <Box variant="code">
          <pre>{JSON.stringify(defaultConfig, null, 2)}</pre>
        </Box>
      </ExpandableSection>
    </SpaceBetween>
  );
};

export default NovaSonicConfigPage;
