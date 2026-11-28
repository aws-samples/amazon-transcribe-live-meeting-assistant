/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/prop-types */
import React, { useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  FormField,
  Input,
  Modal,
  Select,
  SpaceBetween,
  Textarea,
} from '@cloudscape-design/components';

import WhiteboardMarkdown from './WhiteboardMarkdown';
import { LIMITS, SOURCE_ASSISTANT } from './whiteboard-model';
import {
  NEW_PANEL,
  NEW_WHITEBOARD,
  markdownPanels,
  newWhiteboardInput,
  panelsWithText,
  titleFrom,
} from './pin-to-whiteboard';

/** The panel offered first: one whose title the text's title names, else a new panel. */
const defaultPanel = (board, title) => {
  const wanted = (title || '').trim().toLowerCase();
  const match = markdownPanels(board).find((panel) => panel.Title.trim().toLowerCase() === wanted);
  return match ? match.PanelId : NEW_PANEL;
};

/**
 * Adds a piece of text -- an assistant answer pinned from the chat, or a
 * guidance card -- to a whiteboard of the meeting: at the end of one of its
 * Markdown panels, as a new panel, or on a new whiteboard. The text can be
 * edited first. Sent as the signed-in user's change, from the version the page
 * holds; when somebody changed the whiteboard meanwhile, it is read again and
 * the text added to the newer version. `onDone(board, panelTitle)` is called
 * once it is stored.
 */
const PinToWhiteboardModal = ({ whiteboards, text, title, heading = 'Add to whiteboard', onDismiss, onDone }) => {
  const boards = whiteboards.items || [];
  const canCreate = boards.length < (whiteboards.maxWhiteboards || 5);
  const suggested = titleFrom(title || text);
  const [boardId, setBoardId] = useState(boards.length ? boards[boards.length - 1].WhiteboardId : NEW_WHITEBOARD);
  const board = boards.find((b) => b.WhiteboardId === boardId) || null;
  const [panelId, setPanelId] = useState(() => (board ? defaultPanel(board, suggested) : NEW_PANEL));
  const [newTitle, setNewTitle] = useState(suggested);
  const [boardTitle, setBoardTitle] = useState('Pinned');
  const [content, setContent] = useState(String(text || '').trim());
  const [keepWithAssistant, setKeepWithAssistant] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const boardOptions = useMemo(
    () => [
      ...boards.map((b) => ({ value: b.WhiteboardId, label: b.Title })),
      ...(canCreate ? [{ value: NEW_WHITEBOARD, label: 'New whiteboard…' }] : []),
    ],
    [boards, canCreate],
  );
  const panelOptions = board
    ? [
        ...markdownPanels(board).map((p) => ({
          value: p.PanelId,
          label: p.Title,
          description: p.Source === SOURCE_ASSISTANT ? 'Kept by the assistant' : 'Written by a person',
        })),
        { value: NEW_PANEL, label: 'New panel…' },
      ]
    : [];
  const target = board?.Panels?.find((p) => p.PanelId === panelId);
  const toAssistantPanel = target?.Source === SOURCE_ASSISTANT && board?.AssistantUpdates !== false;

  const chooseBoard = (value) => {
    setBoardId(value);
    const chosen = boards.find((b) => b.WhiteboardId === value);
    setPanelId(chosen ? defaultPanel(chosen, suggested) : NEW_PANEL);
    setError(null);
  };

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      if (!board) {
        const result = await whiteboards.create(
          newWhiteboardInput({ title: boardTitle, panelTitle: newTitle, text: content }),
        );
        if (result.error) setError(result.error);
        else onDone(result.board, newTitle.trim() || 'Notes');
        return;
      }
      let current = board;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const change = panelsWithText(current, { panelId, newTitle, text: content, keepWithAssistant });
        if (change.problem) {
          setError(change.problem);
          return;
        }
        // eslint-disable-next-line no-await-in-loop
        const result = await whiteboards.update(current, { Panels: change.panels });
        if (!result.error) {
          onDone(result.board || current, panelId === NEW_PANEL ? newTitle.trim() : target?.Title);
          return;
        }
        if (!result.conflict) {
          setError(result.error);
          return;
        }
        // Somebody changed it meanwhile: add the text to their version instead.
        // eslint-disable-next-line no-await-in-loop
        const fresh = await whiteboards.readVersion(current.WhiteboardId);
        if (!fresh) {
          setError('This whiteboard no longer exists.');
          return;
        }
        current = fresh;
      }
      setError('Somebody kept changing this whiteboard. Try again in a moment.');
    } catch (err) {
      setError('The text could not be added. Try again.');
    } finally {
      setSaving(false);
    }
  };

  const tooLong = content.length > LIMITS.markdown;
  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={heading}
      size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={save}
              loading={saving}
              disabled={!content.trim() || tooLong || (!board && !canCreate)}
            >
              Add
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <FormField label="Whiteboard">
          <Select
            selectedOption={boardOptions.find((o) => o.value === boardId) || null}
            options={boardOptions}
            onChange={({ detail }) => chooseBoard(detail.selectedOption.value)}
            placeholder="Choose a whiteboard"
            ariaLabel="Whiteboard"
          />
        </FormField>
        {!board && (
          <FormField label="New whiteboard's title" constraintText={`At most ${LIMITS.title} characters.`}>
            <Input
              value={boardTitle}
              onChange={({ detail }) => setBoardTitle(detail.value)}
              ariaLabel="New whiteboard's title"
            />
          </FormField>
        )}
        {board && (
          <FormField
            label="Panel"
            description={panelId === NEW_PANEL ? undefined : 'The text is added at the end of this panel.'}
          >
            <Select
              selectedOption={panelOptions.find((o) => o.value === panelId) || null}
              options={panelOptions}
              onChange={({ detail }) => setPanelId(detail.selectedOption.value)}
              ariaLabel="Panel"
            />
          </FormField>
        )}
        {(panelId === NEW_PANEL || !board) && (
          <FormField label="Panel title" constraintText={`At most ${LIMITS.panelTitle} characters.`}>
            <Input value={newTitle} onChange={({ detail }) => setNewTitle(detail.value)} ariaLabel="Panel title" />
          </FormField>
        )}
        <FormField
          label="Text to add"
          constraintText={`Markdown, at most ${LIMITS.markdown} characters.`}
          errorText={tooLong ? `This is ${content.length} characters; shorten it.` : undefined}
        >
          <Textarea
            value={content}
            rows={6}
            onChange={({ detail }) => setContent(detail.value)}
            ariaLabel="Text to add"
          />
        </FormField>
        {toAssistantPanel && (
          <Checkbox checked={keepWithAssistant} onChange={({ detail }) => setKeepWithAssistant(detail.checked)}>
            Let the assistant keep updating this panel (it may reword what you add). Untick to make the panel yours.
          </Checkbox>
        )}
        {content.trim() && !tooLong && (
          <div className="whiteboard-pin-preview" aria-label="Preview">
            <Box variant="small" color="text-body-secondary">
              Preview
            </Box>
            <WhiteboardMarkdown>{content}</WhiteboardMarkdown>
          </div>
        )}
        {!board && !canCreate && (
          <Alert type="info">This meeting has as many whiteboards as it may have. Choose one of them.</Alert>
        )}
        {error && <Alert type="error">{error}</Alert>}
      </SpaceBetween>
    </Modal>
  );
};

export default PinToWhiteboardModal;
