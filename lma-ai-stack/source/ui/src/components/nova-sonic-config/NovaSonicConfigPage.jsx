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

const MEETING_MODE_OPTIONS = [
  { label: 'Normal', value: 'normal' },
  { label: 'Group Meeting', value: 'group' },
  { label: 'Translator', value: 'translator' },
];

const VOICE_ASSISTANT_ACTIVATION_MODE = import.meta.env.VITE_VOICE_ASSISTANT_ACTIVATION_MODE || 'wake_phrase';
const IS_ALWAYS_ACTIVE = VOICE_ASSISTANT_ACTIVATION_MODE === 'always_active';

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

const meetingModeFromData = (data) => {
  if (!data) return 'normal';
  if (data.meetingMode === 'normal' || data.meetingMode === 'group' || data.meetingMode === 'translator') {
    return data.meetingMode;
  }
  if (data.groupMeetingMode === true) return 'group';
  return 'normal';
};

const NovaSonicConfigPage = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [defaultConfig, setDefaultConfig] = useState({});
  // eslint-disable-next-line no-unused-vars
  const [customConfig, setCustomConfig] = useState({});

  const [systemPrompt, setSystemPrompt] = useState('');
  const [promptMode, setPromptMode] = useState(null);
  const [modelId, setModelId] = useState('');
  const [voiceId, setVoiceId] = useState('');
  const [endpointingSensitivity, setEndpointingSensitivity] = useState(null);
  const [meetingMode, setMeetingMode] = useState(MEETING_MODE_OPTIONS[0]);
  const [translatorLanguageA, setTranslatorLanguageA] = useState('');
  const [translatorLanguageB, setTranslatorLanguageB] = useState('');
  const [translatorMutePhrases, setTranslatorMutePhrases] = useState('');
  const [translatorUnmutePhrases, setTranslatorUnmutePhrases] = useState('');

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
        const modeValue = meetingModeFromData(customData);
        setMeetingMode(MEETING_MODE_OPTIONS.find((o) => o.value === modeValue) || MEETING_MODE_OPTIONS[0]);
        setTranslatorLanguageA(customData.translatorLanguageA || '');
        setTranslatorLanguageB(customData.translatorLanguageB || '');
        setTranslatorMutePhrases(customData.translatorMutePhrases || '');
        setTranslatorUnmutePhrases(customData.translatorUnmutePhrases || '');
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
      const modeValue = meetingMode?.value || 'normal';
      configData.meetingMode = modeValue;
      if (modeValue === 'translator') {
        if (translatorLanguageA.trim()) configData.translatorLanguageA = translatorLanguageA.trim();
        if (translatorLanguageB.trim()) configData.translatorLanguageB = translatorLanguageB.trim();
        if (translatorMutePhrases.trim()) configData.translatorMutePhrases = translatorMutePhrases.trim();
        if (translatorUnmutePhrases.trim()) configData.translatorUnmutePhrases = translatorUnmutePhrases.trim();
      }

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
      setMeetingMode(MEETING_MODE_OPTIONS[0]);
      setTranslatorLanguageA('');
      setTranslatorLanguageB('');
      setTranslatorMutePhrases('');
      setTranslatorUnmutePhrases('');
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

  const meetingModeValue = meetingMode?.value || 'normal';
  const meetingModeDisabled = !IS_ALWAYS_ACTIVE && meetingModeValue !== 'normal' ? false : !IS_ALWAYS_ACTIVE;
  const showTranslatorFields = meetingModeValue === 'translator';
  const defaultMeetingModeLabel =
    defaultConfig.meetingMode || (defaultConfig.groupMeetingMode === true ? 'group' : 'normal');

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

          {!IS_ALWAYS_ACTIVE && (
            <Alert type="info">
              Meeting Mode settings (Group Meeting, Translator) only take effect when the virtual participant is
              deployed with Activation Mode set to <b>always_active</b>. Your current deployment uses{' '}
              <b>{VOICE_ASSISTANT_ACTIVATION_MODE}</b>, so these modes will be ignored until the main stack is updated.
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
            label="Meeting Mode"
            description={`Default: ${defaultMeetingModeLabel}. Requires Activation Mode = always_active for 'group' and 'translator' to take effect.`}
          >
            <Select
              selectedOption={meetingMode}
              onChange={({ detail }) => setMeetingMode(detail.selectedOption)}
              options={MEETING_MODE_OPTIONS}
              disabled={meetingModeDisabled}
            />
          </FormField>

          {showTranslatorFields && (
            <SpaceBetween size="l">
              <ColumnLayout columns={2}>
                <FormField
                  label="Translator Language A"
                  description={`Default: ${defaultConfig.translatorLanguageA || 'English'}`}
                >
                  <Input
                    value={translatorLanguageA}
                    onChange={({ detail }) => setTranslatorLanguageA(detail.value)}
                    placeholder={defaultConfig.translatorLanguageA || 'English'}
                    disabled={!IS_ALWAYS_ACTIVE}
                  />
                </FormField>
                <FormField
                  label="Translator Language B"
                  description={`Default: ${defaultConfig.translatorLanguageB || 'Spanish'}`}
                >
                  <Input
                    value={translatorLanguageB}
                    onChange={({ detail }) => setTranslatorLanguageB(detail.value)}
                    placeholder={defaultConfig.translatorLanguageB || 'Spanish'}
                    disabled={!IS_ALWAYS_ACTIVE}
                  />
                </FormField>
              </ColumnLayout>
              <ColumnLayout columns={2}>
                <FormField
                  label="Mute Trigger Phrases"
                  description={
                    'Comma-separated phrases that pause translator mode when spoken. ' +
                    `Default: "${defaultConfig.translatorMutePhrases || 'translator mute, alex mute'}"`
                  }
                >
                  <Input
                    value={translatorMutePhrases}
                    onChange={({ detail }) => setTranslatorMutePhrases(detail.value)}
                    placeholder={defaultConfig.translatorMutePhrases || 'translator mute, alex mute'}
                    disabled={!IS_ALWAYS_ACTIVE}
                  />
                </FormField>
                <FormField
                  label="Unmute Trigger Phrases"
                  description={
                    'Comma-separated phrases that resume translator mode when spoken. ' +
                    `Default: "${defaultConfig.translatorUnmutePhrases || 'translator unmute, alex unmute'}"`
                  }
                >
                  <Input
                    value={translatorUnmutePhrases}
                    onChange={({ detail }) => setTranslatorUnmutePhrases(detail.value)}
                    placeholder={defaultConfig.translatorUnmutePhrases || 'translator unmute, alex unmute'}
                    disabled={!IS_ALWAYS_ACTIVE}
                  />
                </FormField>
              </ColumnLayout>
            </SpaceBetween>
          )}
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
