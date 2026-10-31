/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting's agenda: shown as a numbered list, editable while the meeting
 * runs and after it ends.
 *
 * Editing is offered throughout, unlike the summary, because nothing in the
 * pipeline overwrites an agenda: the only automatic writer is the derivation
 * pass, and it writes only while a meeting has none.
 */
import React, { useState } from 'react';
import {
  Badge,
  Box,
  Button,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
  Textarea,
} from '@cloudscape-design/components';

import { describeAgendaSource, MAX_AGENDA_CHARS, parseAgendaItems } from '../common/agenda';

/* eslint-disable react/prop-types */

const formatUpdated = (updatedBy, updatedAt) => {
  if (!updatedAt) return null;
  const when = new Date(updatedAt);
  const whenText = Number.isNaN(when.getTime()) ? updatedAt : when.toLocaleString();
  return updatedBy ? `Updated by ${updatedBy}, ${whenText}` : `Updated ${whenText}`;
};

const AgendaPanel = ({ item, onSave, readOnly }) => {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);

  const items = parseAgendaItems(item.agenda);
  const sourceLabel = items.length ? describeAgendaSource(item.agendaSource) : null;
  const updated = items.length ? formatUpdated(item.agendaUpdatedBy, item.agendaUpdatedAt) : null;
  const tooLong = draft.length > MAX_AGENDA_CHARS;
  const canEdit = Boolean(onSave) && !readOnly;

  const beginEditing = () => {
    setDraft(item.agenda || '');
    setEditing(true);
  };

  const save = async (text) => {
    setSaving(true);
    try {
      const saved = await onSave(text);
      if (saved) {
        setEditing(false);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Container
      header={
        <Header
          variant="h4"
          description={updated}
          actions={
            <SpaceBetween size="xxs" direction="horizontal" alignItems="center">
              {sourceLabel && !editing && <Badge color="grey">{sourceLabel}</Badge>}
              {saving && <StatusIndicator type="loading">Saving</StatusIndicator>}
              {editing ? (
                <SpaceBetween size="xxs" direction="horizontal">
                  <Button onClick={() => setEditing(false)} disabled={saving}>
                    Cancel
                  </Button>
                  <Button variant="primary" onClick={() => save(draft.trim())} loading={saving} disabled={tooLong}>
                    Save agenda
                  </Button>
                </SpaceBetween>
              ) : (
                canEdit && (
                  <Button iconName="edit" onClick={beginEditing} disabled={saving}>
                    {items.length ? 'Edit' : 'Add agenda'}
                  </Button>
                )
              )}
            </SpaceBetween>
          }
        >
          Agenda
        </Header>
      }
    >
      {editing && (
        <SpaceBetween size="xs">
          <Textarea
            value={draft}
            onChange={({ detail }) => setDraft(detail.value)}
            rows={Math.min(12, Math.max(4, draft.split('\n').length + 1))}
            placeholder={'One item per line, for example:\nIntroductions\nQ3 budget review\nHiring plan'}
            ariaLabel="Agenda, one item per line"
            invalid={tooLong}
          />
          <Box variant="small" color={tooLong ? 'text-status-error' : 'text-body-secondary'}>
            One item per line. Saving an empty agenda clears it.
            {tooLong && ` The agenda is limited to ${MAX_AGENDA_CHARS} characters.`}
          </Box>
        </SpaceBetween>
      )}
      {!editing && items.length > 0 && (
        <ol className="agenda-items" style={{ margin: 0, paddingInlineStart: '1.5em' }}>
          {items.map((text, index) => (
            // Items are plain text lines; two identical lines are two items, so
            // the position is part of the key.
            // eslint-disable-next-line react/no-array-index-key
            <li key={`${index}-${text}`}>{text}</li>
          ))}
        </ol>
      )}
      {!editing && items.length === 0 && <Box color="text-body-secondary">No agenda for this meeting.</Box>}
    </Container>
  );
};

export default AgendaPanel;
