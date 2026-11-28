/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The Skills page editor's playbook section: the whiteboards the playbook
 * makes, the live guidance checks it turns on and how assertive they are, its
 * quick actions, the phrases that suggest it, and a preview of what applying
 * it does. `value` is playbook-model's playbookForm; the section saved is
 * playbookSection(value). See docs/meeting-playbooks.md.
 */
import React from 'react';
import {
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Multiselect,
  Select,
  SpaceBetween,
  Textarea,
  Toggle,
} from '@cloudscape-design/components';
import { TEMPLATES } from '../meeting-whiteboards/whiteboard-model';
import PlaybookSummary from './PlaybookSummary';
import { ASSERTIVENESS, GUIDANCE_CHECKS, LIMITS, describeSection, playbookSection } from './playbook-model';

/* eslint-disable react/prop-types, react/no-array-index-key */

const TEMPLATE_OPTIONS = TEMPLATES.map((t) => ({ value: t.name, label: t.label, description: t.description }));
const CHECK_OPTIONS = GUIDANCE_CHECKS.map((c) => ({ value: c.value, label: c.label, description: c.description }));
const ASSERTIVENESS_OPTIONS = ASSERTIVENESS.map((a) => ({
  value: a.value,
  label: a.label,
  description: a.description,
}));

const SUGGEST_FOR_TEXT =
  "Phrases, separated by commas, that suggest this playbook when a meeting's name, agenda or invitation " +
  'contains them. The display name always does.';
const GUIDANCE_TEXT =
  'Applying the playbook turns live guidance on with only these checks. Fact checks and lookups also need ' +
  "the deployment's own switches.";

const PlaybookSectionEditor = ({ value, onChange, displayName = '', description = '' }) => {
  const set = (patch) => onChange({ ...value, ...patch });
  const setBoard = (index, patch) =>
    set({ whiteboards: value.whiteboards.map((b, i) => (i === index ? { ...b, ...patch } : b)) });
  const setAction = (index, patch) =>
    set({ quickActions: value.quickActions.map((a, i) => (i === index ? { ...a, ...patch } : a)) });
  const guidance = value.guidance || { checks: [], assertiveness: 'balanced', maxItemsPer10Min: '' };
  const setGuidance = (patch) => set({ guidance: { ...guidance, ...patch } });

  return (
    <SpaceBetween size="l">
      <FormField label="Suggested for" description={SUGGEST_FOR_TEXT} stretch>
        <Input
          value={value.suggestForText}
          placeholder="sprint planning, iteration planning"
          ariaLabel="Suggested for"
          onChange={({ detail }) => set({ suggestForText: detail.value })}
        />
      </FormField>

      <Container
        header={
          <Header
            variant="h3"
            description={`Made when the playbook is applied, at most ${LIMITS.whiteboards}. A meeting that already has one of the same title and template keeps it.`}
            actions={
              <Button
                iconName="add-plus"
                disabled={value.whiteboards.length >= LIMITS.whiteboards}
                onClick={() =>
                  set({
                    whiteboards: [
                      ...value.whiteboards,
                      { template: 'decisions', title: '', instructions: '', assistantUpdates: true },
                    ],
                  })
                }
              >
                Add whiteboard
              </Button>
            }
          >
            Whiteboards
          </Header>
        }
      >
        <SpaceBetween size="m">
          {value.whiteboards.length === 0 && <Box color="text-status-inactive">This playbook makes no whiteboard.</Box>}
          {value.whiteboards.map((board, index) => (
            <SpaceBetween size="xs" key={index}>
              <ColumnLayout columns={3}>
                <FormField label="Template">
                  <Select
                    selectedOption={
                      TEMPLATE_OPTIONS.find((o) => o.value === board.template) || {
                        value: board.template,
                        label: `${board.template} (not a template)`,
                      }
                    }
                    options={TEMPLATE_OPTIONS}
                    ariaLabel={`Template of whiteboard ${index + 1}`}
                    onChange={({ detail }) => setBoard(index, { template: detail.selectedOption.value })}
                  />
                </FormField>
                <FormField label="Title" description="Optional; the template's name when empty.">
                  <Input
                    value={board.title}
                    ariaLabel={`Title of whiteboard ${index + 1}`}
                    onChange={({ detail }) => setBoard(index, { title: detail.value })}
                  />
                </FormField>
                <FormField label="Kept up to date">
                  <SpaceBetween direction="horizontal" size="s" alignItems="center">
                    <Toggle
                      checked={board.assistantUpdates}
                      onChange={({ detail }) => setBoard(index, { assistantUpdates: detail.checked })}
                    >
                      By the assistant
                    </Toggle>
                    <Button
                      variant="icon"
                      iconName="remove"
                      ariaLabel={`Remove whiteboard ${index + 1}`}
                      onClick={() => set({ whiteboards: value.whiteboards.filter((_, i) => i !== index) })}
                    />
                  </SpaceBetween>
                </FormField>
              </ColumnLayout>
              <FormField
                label="Instructions for the assistant"
                description={`What this whiteboard is for, up to ${LIMITS.whiteboardInstructions} characters.`}
                stretch
              >
                <Textarea
                  value={board.instructions}
                  rows={2}
                  ariaLabel={`Instructions of whiteboard ${index + 1}`}
                  onChange={({ detail }) => setBoard(index, { instructions: detail.value })}
                />
              </FormField>
            </SpaceBetween>
          ))}
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header variant="h3" description={GUIDANCE_TEXT}>
            Live guidance
          </Header>
        }
      >
        <ColumnLayout columns={3}>
          <FormField label="Checks">
            <Multiselect
              selectedOptions={CHECK_OPTIONS.filter((o) => guidance.checks.includes(o.value))}
              options={CHECK_OPTIONS}
              placeholder="None: live guidance is left as it is"
              ariaLabel="Guidance checks"
              onChange={({ detail }) => setGuidance({ checks: detail.selectedOptions.map((o) => o.value) })}
            />
          </FormField>
          <FormField label="How assertive">
            <Select
              selectedOption={
                ASSERTIVENESS_OPTIONS.find((o) => o.value === guidance.assertiveness) || ASSERTIVENESS_OPTIONS[1]
              }
              options={ASSERTIVENESS_OPTIONS}
              ariaLabel="How assertive"
              disabled={!guidance.checks.length}
              onChange={({ detail }) => setGuidance({ assertiveness: detail.selectedOption.value })}
            />
          </FormField>
          <FormField
            label="Items per 10 minutes at most"
            description="Optional; never more than the deployment allows."
          >
            <Input
              value={guidance.maxItemsPer10Min}
              type="number"
              inputMode="numeric"
              ariaLabel="Items per 10 minutes at most"
              disabled={!guidance.checks.length}
              onChange={({ detail }) => setGuidance({ maxItemsPer10Min: detail.value })}
            />
          </FormField>
        </ColumnLayout>
      </Container>

      <Container
        header={
          <Header
            variant="h3"
            description={`Buttons shown first in the meeting's assistant chat, at most ${LIMITS.quickActions}. Not shown to Participants.`}
            actions={
              <Button
                iconName="add-plus"
                disabled={value.quickActions.length >= LIMITS.quickActions}
                onClick={() => set({ quickActions: [...value.quickActions, { label: '', prompt: '' }] })}
              >
                Add quick action
              </Button>
            }
          >
            Quick actions
          </Header>
        }
      >
        <SpaceBetween size="m">
          {value.quickActions.length === 0 && <Box color="text-status-inactive">No quick actions.</Box>}
          {value.quickActions.map((action, index) => (
            <ColumnLayout columns={2} key={index}>
              <FormField label="Label" description={`Up to ${LIMITS.quickActionLabel} characters.`}>
                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                  <Input
                    value={action.label}
                    ariaLabel={`Label of quick action ${index + 1}`}
                    onChange={({ detail }) => setAction(index, { label: detail.value })}
                  />
                  <Button
                    variant="icon"
                    iconName="remove"
                    ariaLabel={`Remove quick action ${index + 1}`}
                    onClick={() => set({ quickActions: value.quickActions.filter((_, i) => i !== index) })}
                  />
                </SpaceBetween>
              </FormField>
              <FormField label="Prompt" description="Sent to the assistant as the user's question." stretch>
                <Textarea
                  value={action.prompt}
                  rows={2}
                  ariaLabel={`Prompt of quick action ${index + 1}`}
                  onChange={({ detail }) => setAction(index, { prompt: detail.value })}
                />
              </FormField>
            </ColumnLayout>
          ))}
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header variant="h3" description="What applying this playbook to a meeting does.">
            Preview
          </Header>
        }
      >
        <PlaybookSummary playbook={describeSection(playbookSection(value), { displayName, description })} />
      </Container>
    </SpaceBetween>
  );
};

export default PlaybookSectionEditor;
