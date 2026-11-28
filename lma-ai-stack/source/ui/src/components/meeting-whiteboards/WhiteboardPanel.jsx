/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/prop-types */
import React, { useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  Container,
  FormField,
  Header,
  Input,
  SpaceBetween,
  Textarea,
  Toggle,
} from '@cloudscape-design/components';

import MermaidDiagram from './MermaidDiagram';
import WhiteboardMarkdown from './WhiteboardMarkdown';
import { KIND_MERMAID, LIMITS, MERMAID_TYPES, SOURCE_ASSISTANT, panelProblem } from './whiteboard-model';

const DIAGRAM_TYPES = `${MERMAID_TYPES.slice(0, -1).join(', ')} or ${MERMAID_TYPES.at(-1)}`;

const formatTime = (iso) => {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
};

/** A panel's content, drawn for its kind. */
export const PanelContent = ({ panel }) => {
  if (panel.Kind === KIND_MERMAID) return <MermaidDiagram text={panel.Content} />;
  if (!(panel.Content || '').trim()) return <Box color="text-status-inactive">Nothing here yet.</Box>;
  return <WhiteboardMarkdown>{panel.Content}</WhiteboardMarkdown>;
};

/** Who a panel belongs to: the assistant, or the person who last wrote it. */
export const OwnerBadge = ({ panel }) =>
  panel.Source === SOURCE_ASSISTANT ? (
    <Badge color="blue">Assistant</Badge>
  ) : (
    <Badge color="grey">{panel.UpdatedBy ? `Edited by ${panel.UpdatedBy}` : 'Edited by a user'}</Badge>
  );

/**
 * One panel of a whiteboard: its title, who it belongs to, when it changed and
 * its content, with editing in place for those who can change the meeting.
 * Every action resolves to { error, conflict } (see use-meeting-whiteboards).
 */
const WhiteboardPanel = ({ panel, isFirst, isLast, readOnly, assistantOn, onSave, onHandBack, onMove, onRemove }) => {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(panel.Title);
  const [content, setContent] = useState(panel.Content || '');
  const [preview, setPreview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const isDiagram = panel.Kind === KIND_MERMAID;

  const startEditing = () => {
    setTitle(panel.Title);
    setContent(panel.Content || '');
    setPreview(false);
    setError(null);
    setEditing(true);
  };

  const save = async () => {
    const problem = panelProblem({ Title: title, Kind: panel.Kind, Content: content });
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    const result = await onSave({ Title: title.trim(), Content: content });
    setSaving(false);
    if (result?.error) setError(result.error);
    else setEditing(false);
  };

  const onMenu = async ({ detail }) => {
    const actions = {
      edit: startEditing,
      handBack: onHandBack,
      up: () => onMove(-1),
      down: () => onMove(1),
      remove: onRemove,
    };
    await actions[detail.id]?.();
  };

  const menu = readOnly ? null : (
    <SpaceBetween direction="horizontal" size="xs">
      <Button variant="inline-icon" iconName="edit" ariaLabel={`Edit ${panel.Title}`} onClick={startEditing} />
      <ButtonDropdown
        variant="icon"
        ariaLabel={`More actions for ${panel.Title}`}
        expandToViewport
        onItemClick={onMenu}
        items={[
          {
            id: 'handBack',
            text: 'Let the assistant update this panel',
            disabled: panel.Source === SOURCE_ASSISTANT || !assistantOn,
            disabledReason:
              panel.Source === SOURCE_ASSISTANT
                ? 'The assistant already updates this panel.'
                : 'The assistant does not update this whiteboard.',
          },
          { id: 'up', text: 'Move up', disabled: isFirst },
          { id: 'down', text: 'Move down', disabled: isLast },
          { id: 'remove', text: 'Remove panel' },
        ]}
      />
    </SpaceBetween>
  );

  return (
    <Container
      header={
        <Header
          variant="h3"
          actions={editing ? null : menu}
          description={
            <SpaceBetween direction="horizontal" size="xs">
              <OwnerBadge panel={panel} />
              <span>{formatTime(panel.UpdatedAt)}</span>
            </SpaceBetween>
          }
        >
          {panel.Title}
        </Header>
      }
    >
      {editing ? (
        <SpaceBetween size="s">
          <FormField label="Title" constraintText={`At most ${LIMITS.panelTitle} characters.`}>
            <Input value={title} onChange={({ detail }) => setTitle(detail.value)} ariaLabel="Panel title" />
          </FormField>
          <FormField
            label={isDiagram ? 'Diagram (Mermaid)' : 'Content (Markdown)'}
            constraintText={
              isDiagram
                ? `At most ${LIMITS.mermaid} characters. Starts with ${DIAGRAM_TYPES}.`
                : `At most ${LIMITS.markdown} characters.`
            }
          >
            <Textarea
              value={content}
              rows={isDiagram ? 10 : 8}
              onChange={({ detail }) => setContent(detail.value)}
              ariaLabel="Panel content"
            />
          </FormField>
          <Toggle checked={preview} onChange={({ detail }) => setPreview(detail.checked)}>
            Preview
          </Toggle>
          {preview && <PanelContent panel={{ ...panel, Content: content }} />}
          {panel.Source === SOURCE_ASSISTANT && (
            <Box variant="small" color="text-status-inactive">
              Once you save a change, this panel is yours and the assistant no longer changes it.
            </Box>
          )}
          {error && <Alert type="error">{error}</Alert>}
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={save} loading={saving}>
              Save
            </Button>
          </SpaceBetween>
        </SpaceBetween>
      ) : (
        <PanelContent panel={panel} />
      )}
    </Container>
  );
};

export default WhiteboardPanel;
