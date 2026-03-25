/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  FormField,
  Input,
  Textarea,
  Button,
  Alert,
  Spinner,
  Box,
  ExpandableSection,
  Icon,
} from '@awsui/components-react';
import { API, graphqlOperation } from 'aws-amplify';

const getLLMPromptTemplateQuery = `
  query GetLLMPromptTemplate($LLMPromptTemplateId: ID!) {
    getLLMPromptTemplate(LLMPromptTemplateId: $LLMPromptTemplateId) {
      LLMPromptTemplateId
    }
  }
`;

const updateLLMPromptTemplateMutation = `
  mutation UpdateLLMPromptTemplate($input: UpdateLLMPromptTemplateInput!) {
    updateLLMPromptTemplate(input: $input) {
      LLMPromptTemplateId
      Success
    }
  }
`;

// Parse config object into sorted array of {number, label, prompt} entries
const parseTemplateConfig = (config) => {
  if (!config) return [];
  const entries = [];
  Object.keys(config).forEach((key) => {
    const match = key.match(/^(\d+)#(.+)$/);
    if (match) {
      entries.push({
        key,
        number: parseInt(match[1], 10),
        label: match[2],
        prompt: config[key],
      });
    }
  });
  return entries.sort((a, b) => a.number - b.number);
};

const TranscriptSummaryPage = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [defaultConfig, setDefaultConfig] = useState({});
  const [templates, setTemplates] = useState([]);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [defaultResult, customResult] = await Promise.all([
        API.graphql(
          graphqlOperation(getLLMPromptTemplateQuery, { LLMPromptTemplateId: 'DefaultSummaryPromptTemplates' }),
        ),
        API.graphql(
          graphqlOperation(getLLMPromptTemplateQuery, { LLMPromptTemplateId: 'CustomSummaryPromptTemplates' }),
        ),
      ]);

      const defaultData = JSON.parse(defaultResult.data.getLLMPromptTemplate.LLMPromptTemplateId);
      const customData = JSON.parse(customResult.data.getLLMPromptTemplate.LLMPromptTemplateId);

      setDefaultConfig(defaultData || {});

      // If custom config has template entries, use those; otherwise use defaults
      const customEntries = parseTemplateConfig(customData);
      if (customEntries.length > 0) {
        setTemplates(customEntries);
      } else {
        setTemplates(parseTemplateConfig(defaultData));
      }
    } catch (err) {
      console.error('Error loading LLM prompt templates:', err);
      setError('Failed to load configuration. Please try again.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const handleLabelChange = (index, newLabel) => {
    const updated = [...templates];
    updated[index] = { ...updated[index], label: newLabel };
    setTemplates(updated);
  };

  const handlePromptChange = (index, newPrompt) => {
    const updated = [...templates];
    updated[index] = { ...updated[index], prompt: newPrompt };
    setTemplates(updated);
  };

  const handleAddTemplate = () => {
    const maxNumber = templates.reduce((max, t) => Math.max(max, t.number), 0);
    setTemplates([
      ...templates,
      {
        key: `${maxNumber + 1}#NEW_TEMPLATE`,
        number: maxNumber + 1,
        label: 'NEW_TEMPLATE',
        prompt: 'Enter your prompt template here. Use {transcript} as placeholder for the meeting transcript.',
      },
    ]);
  };

  const handleDeleteTemplate = (index) => {
    const updated = templates.filter((_, i) => i !== index);
    setTemplates(updated);
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const configData = {};
      templates.forEach((template, index) => {
        const key = `${index + 1}#${template.label}`;
        configData[key] = template.prompt;
      });

      await API.graphql(
        graphqlOperation(updateLLMPromptTemplateMutation, {
          input: {
            LLMPromptTemplateId: 'CustomSummaryPromptTemplates',
            TemplateConfig: JSON.stringify(configData),
          },
        }),
      );

      setSuccess('Summary prompt templates saved successfully.');
      await loadConfig();
    } catch (err) {
      console.error('Error saving LLM prompt templates:', err);
      setError('Failed to save templates. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  const handleResetToDefaults = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await API.graphql(
        graphqlOperation(updateLLMPromptTemplateMutation, {
          input: {
            LLMPromptTemplateId: 'CustomSummaryPromptTemplates',
            TemplateConfig: JSON.stringify({}),
          },
        }),
      );

      setTemplates(parseTemplateConfig(defaultConfig));
      setSuccess('Custom overrides cleared. Default templates will be used.');
      await loadConfig();
    } catch (err) {
      console.error('Error resetting templates:', err);
      setError('Failed to reset templates. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Container header={<Header variant="h1">Transcript Summary Prompts</Header>}>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" /> Loading templates...
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
              'Customize the summary prompt templates used for end-of-meeting transcript ' +
              'summarization. Changes will apply to all future meeting summaries. ' +
              'Set a prompt to "NONE" to disable a default template.'
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={handleResetToDefaults} loading={saving}>
                  Reset to Defaults
                </Button>
                <Button onClick={() => handleSave()} disabled={saving}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={handleSave} loading={saving}>
                  Save Changes
                </Button>
              </SpaceBetween>
            }
          >
            Transcript Summary Prompts
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

          {templates.map((template, index) => (
            <Container
              key={`template-${template.number}-${template.label}`}
              header={
                <Header
                  variant="h3"
                  actions={
                    <Button
                      iconName="remove"
                      variant="icon"
                      onClick={() => handleDeleteTemplate(index)}
                      ariaLabel={`Delete template ${index + 1}`}
                    />
                  }
                >
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>Template {index + 1}</span>
                    <Icon name="remove" />
                  </SpaceBetween>
                </Header>
              }
            >
              <SpaceBetween size="m">
                <FormField label="Label">
                  <Input
                    value={template.label}
                    onChange={({ detail }) => handleLabelChange(index, detail.value)}
                    placeholder="e.g., SUMMARY, DETAILS, ACTIONS"
                  />
                </FormField>
                <FormField label='Prompt (set to "NONE" to disable this template)'>
                  <Textarea
                    value={template.prompt}
                    onChange={({ detail }) => handlePromptChange(index, detail.value)}
                    placeholder="Enter prompt template... Use {transcript} as placeholder."
                    rows={6}
                  />
                </FormField>
              </SpaceBetween>
            </Container>
          ))}

          <Button iconName="add-plus" onClick={handleAddTemplate}>
            Add Template
          </Button>
        </SpaceBetween>
      </Container>

      <ExpandableSection headerText="View Default Templates (read-only)" variant="container">
        <Box variant="code">
          <pre>{JSON.stringify(defaultConfig, null, 2)}</pre>
        </Box>
      </ExpandableSection>
    </SpaceBetween>
  );
};

export default TranscriptSummaryPage;
