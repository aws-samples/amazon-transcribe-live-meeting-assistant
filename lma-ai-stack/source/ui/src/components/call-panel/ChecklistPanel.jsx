/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting's checklist: the questions, topics, risks, confirmations,
 * decisions and actions it should cover, each with its status, who set it and
 * when, its evidence (links to the transcript time) and a note.
 *
 * Those who can change the meeting set an item's status and note here, and
 * edit the list itself: add, change, reorder and remove items. Saving the list
 * keeps each remaining item's status, evidence and note, unless "Start every
 * item again as open" is chosen. A status set here is a user's, which the
 * assistant never changes. Never posted to the meeting chat or spoken by LMA.
 * See docs/meeting-context-and-checklist.md.
 *
 * Hidden when the meeting has none and the viewer cannot add one. On the
 * Transcript tab it is `collapsible`, open while the meeting is live.
 */
import React, { useEffect, useState } from 'react';
import {
  Badge,
  Box,
  Button,
  Checkbox,
  Container,
  ExpandableSection,
  Header,
  Input,
  Select,
  SpaceBetween,
  StatusIndicator,
  Textarea,
} from '@cloudscape-design/components';

import {
  checklistDefinitions,
  checklistProblem,
  formatElapsed,
  formatUpdated,
  KIND_OPTIONS,
  MAX_CHECKLIST_ITEMS,
  MAX_NOTE_CHARS,
  moveItem,
  nextItemId,
  optionFor,
  PRIORITY_OPTIONS,
  STATUS_OPTIONS,
  statusDisplay,
  statusSummary,
} from '../common/meeting-preparation';

/* eslint-disable react/prop-types */

const whoSetIt = (item) => {
  const updated = formatUpdated(item.UpdatedBy, item.UpdatedAt);
  if (!updated) return null;
  return item.StatusSource === 'assistant' ? `${updated} (status from the assistant)` : updated;
};

const Evidence = ({ evidence, onShowTime }) => {
  const entries = (evidence || []).filter((entry) => entry && (entry.StartTime !== null || entry.Quote));
  if (!entries.length) return null;
  return (
    <Box variant="small" color="text-body-secondary">
      <SpaceBetween size="xxs">
        {entries.map((entry, index) => {
          const time =
            entry.StartTime === null || entry.StartTime === undefined ? null : formatElapsed(entry.StartTime);
          return (
            // Entries have no id of their own; their order is stable.
            // eslint-disable-next-line react/no-array-index-key
            <span key={`${entry.SegmentId || ''}-${index}`}>
              {'Evidence '}
              {time && onShowTime ? (
                <Button
                  variant="inline-link"
                  onClick={() => onShowTime(Number(entry.StartTime))}
                  ariaLabel={`Show the transcript at ${time}`}
                >
                  {`at ${time}`}
                </Button>
              ) : (
                time && `at ${time}`
              )}
              {entry.Quote && ` "${entry.Quote}"`}
            </span>
          );
        })}
      </SpaceBetween>
    </Box>
  );
};

const ChecklistItemView = ({ item, canEdit, busy, onStatus, onNote, onShowTime }) => {
  const [editingNote, setEditingNote] = useState(false);
  const [note, setNote] = useState('');
  const display = statusDisplay(item.Status);
  const who = whoSetIt(item);

  const saveNote = async () => {
    if (await onNote(note)) setEditingNote(false);
  };

  return (
    <li className="checklist-item" data-testid={`checklist-item-${item.ItemId}`}>
      <SpaceBetween size="xxs">
        <SpaceBetween size="xs" direction="horizontal" alignItems="center">
          {canEdit ? (
            <Select
              selectedOption={optionFor(STATUS_OPTIONS, item.Status)}
              options={STATUS_OPTIONS}
              onChange={({ detail }) => onStatus(detail.selectedOption.value)}
              disabled={busy}
              ariaLabel={`Status of ${item.Text}`}
              expandToViewport
            />
          ) : (
            <StatusIndicator type={display.type}>{display.label}</StatusIndicator>
          )}
          <Badge color={item.Priority === 'must' ? 'red' : 'grey'}>{item.Priority || 'should'}</Badge>
          <Badge color="grey">{item.Kind}</Badge>
          <span>{item.Text}</span>
        </SpaceBetween>
        {who && (
          <Box variant="small" color="text-body-secondary">
            {who}
          </Box>
        )}
        <Evidence evidence={item.Evidence} onShowTime={onShowTime} />
        {!editingNote && item.Note && <Box variant="small">{`Note: ${item.Note}`}</Box>}
        {editingNote && (
          <SpaceBetween size="xxs">
            <Textarea
              value={note}
              onChange={({ detail }) => setNote(detail.value)}
              rows={3}
              ariaLabel={`Note on ${item.Text}`}
              invalid={note.length > MAX_NOTE_CHARS}
            />
            <SpaceBetween size="xxs" direction="horizontal">
              <Button onClick={() => setEditingNote(false)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={saveNote} loading={busy} disabled={note.length > MAX_NOTE_CHARS}>
                Save note
              </Button>
            </SpaceBetween>
          </SpaceBetween>
        )}
        {canEdit && !editingNote && (
          <Box>
            <Button
              variant="inline-link"
              onClick={() => {
                setNote(item.Note || '');
                setEditingNote(true);
              }}
              disabled={busy}
            >
              {item.Note ? 'Edit note' : 'Add note'}
            </Button>
          </Box>
        )}
      </SpaceBetween>
    </li>
  );
};

/**
 * The checklist as a list of editable items: text, kind and priority, moved up
 * and down, removed and added. Also the Virtual Participant forms' checklist,
 * which has no statuses to start again (no `setReset`).
 */
export const ChecklistEditor = ({ draft, setDraft, reset = false, setReset = null }) => {
  const change = (index, patch) => setDraft(draft.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  return (
    <SpaceBetween size="s">
      {draft.map((item, index) => (
        <div key={item.ItemId} data-testid={`checklist-edit-${item.ItemId}`}>
          <SpaceBetween size="xs" direction="horizontal" alignItems="center">
            <Box variant="small" color="text-body-secondary">
              {index + 1}
            </Box>
            <div style={{ minWidth: '18rem' }}>
              <Input
                value={item.Text}
                onChange={({ detail }) => change(index, { Text: detail.value })}
                placeholder="What the meeting should cover"
                ariaLabel={`Item ${index + 1} text`}
              />
            </div>
            <Select
              selectedOption={optionFor(KIND_OPTIONS, item.Kind)}
              options={KIND_OPTIONS}
              onChange={({ detail }) => change(index, { Kind: detail.selectedOption.value })}
              ariaLabel={`Item ${index + 1} kind`}
              expandToViewport
            />
            <Select
              selectedOption={optionFor(PRIORITY_OPTIONS, item.Priority || 'should')}
              options={PRIORITY_OPTIONS}
              onChange={({ detail }) => change(index, { Priority: detail.selectedOption.value })}
              ariaLabel={`Item ${index + 1} priority`}
              expandToViewport
            />
            <Button
              iconName="angle-up"
              variant="icon"
              ariaLabel={`Move item ${index + 1} up`}
              disabled={index === 0}
              onClick={() => setDraft(moveItem(draft, index, -1))}
            />
            <Button
              iconName="angle-down"
              variant="icon"
              ariaLabel={`Move item ${index + 1} down`}
              disabled={index === draft.length - 1}
              onClick={() => setDraft(moveItem(draft, index, 1))}
            />
            <Button
              iconName="remove"
              variant="icon"
              ariaLabel={`Remove item ${index + 1}`}
              onClick={() => setDraft(draft.filter((_, i) => i !== index))}
            />
          </SpaceBetween>
        </div>
      ))}
      <SpaceBetween size="s" direction="horizontal" alignItems="center">
        <Button
          iconName="add-plus"
          disabled={draft.length >= MAX_CHECKLIST_ITEMS}
          onClick={() =>
            setDraft([...draft, { ItemId: nextItemId(draft), Text: '', Kind: 'topic', Priority: 'should' }])
          }
        >
          Add item
        </Button>
        {setReset && (
          <Checkbox checked={reset} onChange={({ detail }) => setReset(detail.checked)}>
            Start every item again as open
          </Checkbox>
        )}
      </SpaceBetween>
    </SpaceBetween>
  );
};

const ChecklistPanel = ({
  item,
  onSaveList,
  onUpdateItem,
  onShowTime,
  readOnly,
  collapsible = false,
  defaultExpanded = false,
}) => {
  const items = Array.isArray(item.checklist) ? item.checklist.filter(Boolean) : [];
  const canEdit = Boolean(onSaveList && onUpdateItem) && !readOnly;
  const [expandedChoice, setExpandedChoice] = useState(null);
  useEffect(() => setExpandedChoice(null), [item.callId]);
  const expanded = expandedChoice ?? defaultExpanded;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState([]);
  const [reset, setReset] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  if (!items.length && !canEdit) return null;

  const run = async (change) => {
    setBusy(true);
    setError('');
    try {
      const result = await change();
      if (!result?.ok) setError(result?.message || 'The checklist could not be saved.');
      return Boolean(result?.ok);
    } finally {
      setBusy(false);
    }
  };

  const beginEditing = () => {
    setDraft(checklistDefinitions(items));
    setReset(false);
    setError('');
    setEditing(true);
    setExpandedChoice(true);
  };

  const problem = editing ? checklistProblem(draft) : null;
  const saveList = async () => {
    if (await run(() => onSaveList(checklistDefinitions(draft), reset))) setEditing(false);
  };

  const summary = statusSummary(items);
  const actions = editing ? (
    <SpaceBetween size="xxs" direction="horizontal">
      <Button onClick={() => setEditing(false)} disabled={busy}>
        Cancel
      </Button>
      <Button variant="primary" onClick={saveList} loading={busy} disabled={Boolean(problem)}>
        Save checklist
      </Button>
    </SpaceBetween>
  ) : (
    canEdit && (
      <Button iconName="edit" onClick={beginEditing} disabled={busy}>
        {items.length ? 'Edit list' : 'Add checklist'}
      </Button>
    )
  );

  const body = (
    <SpaceBetween size="s">
      {error && <StatusIndicator type="error">{error}</StatusIndicator>}
      {editing && problem && <StatusIndicator type="warning">{problem}</StatusIndicator>}
      {editing && <ChecklistEditor draft={draft} setDraft={setDraft} reset={reset} setReset={setReset} />}
      {!editing && items.length > 0 && (
        <ol className="checklist-items" style={{ margin: 0, paddingInlineStart: '1.5em' }}>
          {items.map((checklistItem) => (
            <ChecklistItemView
              key={checklistItem.ItemId}
              item={checklistItem}
              canEdit={canEdit}
              busy={busy}
              onShowTime={onShowTime}
              onStatus={(status) => run(() => onUpdateItem(checklistItem.ItemId, { Status: status }))}
              onNote={(note) => run(() => onUpdateItem(checklistItem.ItemId, { Note: note }))}
            />
          ))}
        </ol>
      )}
      {!editing && !items.length && <Box color="text-body-secondary">No checklist for this meeting.</Box>}
    </SpaceBetween>
  );

  if (collapsible) {
    return (
      <div data-testid="checklist-collapsible">
        <ExpandableSection
          variant="container"
          headerText="Checklist"
          headerCounter={items.length ? `(${items.length})` : undefined}
          headerDescription={summary || undefined}
          headerActions={actions}
          expanded={expanded}
          onChange={({ detail }) => setExpandedChoice(detail.expanded)}
        >
          {body}
        </ExpandableSection>
      </div>
    );
  }

  return (
    <div data-testid="checklist-panel">
      <Container
        header={
          <Header
            variant="h4"
            counter={items.length ? `(${items.length})` : undefined}
            description={summary}
            actions={actions}
          >
            Checklist
          </Header>
        }
      >
        {body}
      </Container>
    </div>
  );
};

export default ChecklistPanel;
