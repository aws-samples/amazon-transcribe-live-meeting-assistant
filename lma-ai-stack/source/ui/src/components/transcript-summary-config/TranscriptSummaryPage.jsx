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
  Button,
  Alert,
  Spinner,
  Box,
  ExpandableSection,
  Icon,
} from '@cloudscape-design/components';

import AdminOnly from '../common/AdminOnly';
import { isConflict } from '../common/config-conflict';

const client = generateClient();
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
      Version
    }
  }
`;

// Shown when the stored record moved on while this page was open -- another
// administrator saved it. The save is refused rather than replacing their edit,
// so the only way forward is to reload and reapply.
const CONFLICT_MESSAGE =
  'Someone else saved these prompts while this page was open. Reload the page to see ' +
  'their version, then apply your changes again.';

// Shared with the other configuration pages, and re-exported so this page's
// tests keep asserting the match itself.
export { isConflict };

// A prompt is stored under a key of this form, where the number orders the
// sections of the summary. A stored record also carries its version and who
// saved it, and this is what keeps those out of the editor.
const PROMPT_KEY = /^(\d+)#(.+)$/;

// Parse config object into sorted array of {number, label, prompt} entries
const parseTemplateConfig = (config) => {
  if (!config) return [];
  const entries = [];
  Object.keys(config).forEach((key) => {
    const match = key.match(PROMPT_KEY);
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

/** Just the prompts from a stored record, without its version or provenance. */
export const promptsOnly = (config) =>
  Object.fromEntries(Object.entries(config || {}).filter(([key]) => PROMPT_KEY.test(key)));

/**
 * What the summarizer will actually use: the shipped prompts with this
 * deployment's edits over them.
 *
 * The stored profile holds only the sections an administrator changed, so the
 * editor has to be filled from the merge rather than from either record alone.
 * A section whose prompt is empty or `NONE` is one that was removed -- the
 * summarizer skips it -- so it is left out here too, and the page shows what
 * will run rather than what is stored.
 */
export const mergeTemplateConfig = (defaultConfig, customConfig) => {
  const merged = {};
  [defaultConfig, customConfig].forEach((config) => {
    Object.entries(config || {}).forEach(([key, value]) => {
      if (PROMPT_KEY.test(key)) merged[key] = value;
    });
  });
  Object.keys(merged).forEach((key) => {
    if (!merged[key] || merged[key] === 'NONE') delete merged[key];
  });
  return merged;
};

/**
 * What to store for this deployment: the sections that differ from the shipped
 * ones, and nothing else.
 *
 * Storing a section the deployment already ships, unchanged, would pin it: a
 * later release that improves that prompt would never reach this deployment,
 * because the stored copy wins. So an unchanged section is left out and keeps
 * coming from the release.
 *
 * A section keeps the number it was loaded under rather than taking the one its
 * position on the page implies. That is what makes the sentence above true after
 * a deletion: numbering by position would renumber every section after the
 * deleted one, none of those new keys would match the shipped record, and their
 * unchanged text would be stored as a pinned copy. Only a section's own label
 * and prompt decide whether it is stored.
 *
 * A shipped section that is no longer on the page was removed, and leaving it
 * out would let the release supply it again. It is stored as `NONE`, which is
 * how a section is suppressed -- the summarizer skips it, and skipping it also
 * saves a model call. Renaming a section does the same to the name it had,
 * because the label is part of the key.
 */
export const buildCustomTemplateConfig = (templates, defaultConfig) => {
  const config = {};
  const onThePage = new Set();
  // A section added on the page carries its own number; one that somehow does
  // not gets the next free one rather than its position, which could repeat a
  // number a loaded section already holds and leave the order of the two
  // depending on how the tie breaks.
  let nextFree = templates.reduce(
    (highest, template) => (Number.isInteger(template.number) ? Math.max(highest, template.number) : highest),
    0,
  );
  templates.forEach((template) => {
    let { number } = template;
    if (!Number.isInteger(number)) {
      nextFree += 1;
      number = nextFree;
    }
    const key = `${number}#${template.label}`;
    onThePage.add(key);
    if ((defaultConfig || {})[key] !== template.prompt) {
      config[key] = template.prompt;
    }
  });
  Object.keys(defaultConfig || {})
    .filter((key) => PROMPT_KEY.test(key) && !onThePage.has(key))
    .forEach((key) => {
      config[key] = 'NONE';
    });
  return config;
};

const TranscriptSummaryPageContent = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [defaultConfig, setDefaultConfig] = useState({});
  const [templates, setTemplates] = useState([]);
  // The version of the stored custom record this editor was filled from. Sent
  // back on save so a save against a record someone else has since changed is
  // refused instead of overwriting it.
  const [version, setVersion] = useState(null);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [defaultResult, customResult] = await Promise.all([
        client.graphql({
          query: getLLMPromptTemplateQuery,
          variables: { LLMPromptTemplateId: 'DefaultSummaryPromptTemplates' },
        }),
        client.graphql({
          query: getLLMPromptTemplateQuery,
          variables: { LLMPromptTemplateId: 'CustomSummaryPromptTemplates' },
        }),
      ]);

      const defaultData = JSON.parse(defaultResult.data.getLLMPromptTemplate.LLMPromptTemplateId);
      const customData = JSON.parse(customResult.data.getLLMPromptTemplate.LLMPromptTemplateId);

      setDefaultConfig(defaultData || {});
      setVersion(typeof customData?.Version === 'number' ? customData.Version : null);

      setTemplates(parseTemplateConfig(mergeTemplateConfig(defaultData, customData)));
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
      const configData = buildCustomTemplateConfig(templates, defaultConfig);

      await client.graphql({
        query: updateLLMPromptTemplateMutation,
        variables: {
          input: {
            LLMPromptTemplateId: 'CustomSummaryPromptTemplates',
            TemplateConfig: JSON.stringify(configData),
            ExpectedVersion: version,
          },
        },
      });

      setSuccess('Summary prompt templates saved successfully.');
      await loadConfig();
    } catch (err) {
      console.error('Error saving LLM prompt templates:', err);
      setError(isConflict(err) ? CONFLICT_MESSAGE : 'Failed to save templates. Please try again.');
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
        query: updateLLMPromptTemplateMutation,
        variables: {
          input: {
            LLMPromptTemplateId: 'CustomSummaryPromptTemplates',
            TemplateConfig: JSON.stringify({}),
            ExpectedVersion: version,
          },
        },
      });

      setTemplates(parseTemplateConfig(defaultConfig));
      setSuccess('Custom overrides cleared. Default templates will be used.');
      await loadConfig();
    } catch (err) {
      console.error('Error resetting templates:', err);
      setError(isConflict(err) ? CONFLICT_MESSAGE : 'Failed to reset templates. Please try again.');
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
              'summarization. A saved change applies to meeting summaries generated from then ' +
              'on, within about five minutes; Regenerate on a meeting page uses it at once. ' +
              'Only the sections you change are stored, so later releases keep improving the ' +
              'rest. Set a prompt to "NONE", or delete it, to turn a shipped section off; ' +
              'Reset to Defaults brings every shipped section back.'
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={handleResetToDefaults} loading={saving}>
                  Reset to Defaults
                </Button>
                {/* Discards the page's edits by reloading what is stored.
                    It never saves: a Cancel that saved could answer with the
                    message telling an administrator that somebody else saved
                    first, which is not a thing to be told after cancelling. */}
                <Button onClick={loadConfig} disabled={saving}>
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
          {/* Only the prompts. The record also carries its version and who
              saved it, which is not a template and would read as one here. */}
          <pre>{JSON.stringify(promptsOnly(defaultConfig), null, 2)}</pre>
        </Box>
      </ExpandableSection>
    </SpaceBetween>
  );
};

// The summary prompts apply to every meeting summary in this account, so the page
// is for Admin users; everyone else is told so instead of being shown a form whose
// save would be refused.
const TranscriptSummaryPage = () => (
  <AdminOnly feature="the transcript summary prompts">
    <TranscriptSummaryPageContent />
  </AdminOnly>
);

export default TranscriptSummaryPage;
