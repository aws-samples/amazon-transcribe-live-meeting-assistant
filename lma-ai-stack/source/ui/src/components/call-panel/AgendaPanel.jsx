/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting's agenda: shown as a numbered list, editable while the meeting
 * runs and after it ends.
 *
 * Editing is offered throughout, unlike the summary: the end-of-meeting
 * pipeline replaces a summary when the meeting ends, while a stored agenda is
 * replaced only by somebody saving a new one or pressing Derive agenda. The
 * automatic derivation pass writes only while a meeting has no agenda.
 */
import React, { useEffect, useState } from 'react';
import {
  Alert,
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

/** How long the outcome of Derive agenda stays in the panel header. */
const DERIVE_STATUS_VISIBLE_MS = 20000;

const formatUpdated = (updatedBy, updatedAt) => {
  if (!updatedAt) return null;
  const when = new Date(updatedAt);
  const whenText = Number.isNaN(when.getTime()) ? updatedAt : when.toLocaleString();
  return updatedBy ? `Updated by ${updatedBy}, ${whenText}` : `Updated ${whenText}`;
};

/** What to say about a derivation's outcome, from the backend's answer. */
export const deriveStatus = (result) => {
  if (!result?.Success) {
    return { type: 'error', text: result?.Message || 'could not derive an agenda' };
  }
  if (result.Derived) {
    return { type: 'success', text: 'Agenda derived from the transcript' };
  }
  return { type: 'info', text: result.Message || 'no agenda was stated in the meeting' };
};

const AgendaPanel = ({ item, onSave, onDerive, readOnly }) => {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [deriving, setDeriving] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState(false);
  const [status, setStatus] = useState(null);

  const items = parseAgendaItems(item.agenda);
  const sourceLabel = items.length ? describeAgendaSource(item.agendaSource) : null;
  const updated = items.length ? formatUpdated(item.agendaUpdatedBy, item.agendaUpdatedAt) : null;
  const tooLong = draft.length > MAX_AGENDA_CHARS;
  const canEdit = Boolean(onSave) && !readOnly;
  const canDerive = Boolean(onDerive) && !readOnly;

  useEffect(() => {
    if (!status) return undefined;
    const timer = setTimeout(() => setStatus(null), DERIVE_STATUS_VISIBLE_MS);
    return () => clearTimeout(timer);
  }, [status]);

  const beginEditing = () => {
    setDraft(item.agenda || '');
    setConfirmReplace(false);
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

  const derive = async () => {
    setConfirmReplace(false);
    setStatus(null);
    setDeriving(true);
    try {
      setStatus(deriveStatus(await onDerive()));
    } finally {
      setDeriving(false);
    }
  };

  const handleDeriveClick = () => {
    // An agenda somebody gave -- on the scheduling form, through an MCP client
    // or on this page -- is their own, and deriving replaces it, so that case
    // asks first. A derived one has nothing to lose, and neither does none.
    if (items.length && item.agendaSource !== 'derived') {
      setConfirmReplace(true);
      return;
    }
    derive();
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
              {deriving && <StatusIndicator type="loading">Reading the transcript</StatusIndicator>}
              {!deriving && status && (
                // Announced, because the outcome -- especially "no agenda was
                // stated" -- is the whole feedback for pressing the button.
                <Box nativeAttributes={{ role: 'status', 'aria-live': 'polite' }}>
                  <StatusIndicator type={status.type}>{status.text}</StatusIndicator>
                </Box>
              )}
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
                <SpaceBetween size="xxs" direction="horizontal">
                  {canDerive && (
                    <Button iconName="search" onClick={handleDeriveClick} loading={deriving} disabled={saving}>
                      Derive agenda
                    </Button>
                  )}
                  {canEdit && (
                    <Button iconName="edit" onClick={beginEditing} disabled={saving || deriving}>
                      {items.length ? 'Edit' : 'Add agenda'}
                    </Button>
                  )}
                </SpaceBetween>
              )}
            </SpaceBetween>
          }
        >
          Agenda
        </Header>
      }
    >
      <SpaceBetween size="s">
        {confirmReplace && (
          <Alert
            type="warning"
            action={
              <SpaceBetween size="xxs" direction="horizontal">
                <Button onClick={() => setConfirmReplace(false)}>Keep it</Button>
                <Button onClick={derive}>Replace</Button>
              </SpaceBetween>
            }
          >
            This meeting already has an agenda. Deriving replaces it with the agenda stated in the meeting, if one was
            stated; if none was, the current agenda is kept.
          </Alert>
        )}
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
      </SpaceBetween>
    </Container>
  );
};

export default AgendaPanel;
