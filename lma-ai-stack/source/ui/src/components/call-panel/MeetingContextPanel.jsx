/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting's context: a Markdown briefing for the assistant and the
 * summary -- goals, background, terms, what a good outcome looks like. Shown to
 * the people who can open the meeting; never posted to the meeting chat or
 * spoken by LMA. See docs/meeting-context-and-checklist.md.
 *
 * Hidden when the meeting has none and the viewer cannot add one.
 */
import React, { useState } from 'react';
import { Box, Button, Container, Header, SpaceBetween, StatusIndicator, Textarea } from '@cloudscape-design/components';

import { formatUpdated, MAX_CONTEXT_CHARS } from '../common/meeting-preparation';
import SummaryMarkdown from '../summary-markdown/SummaryMarkdown';

/* eslint-disable react/prop-types */

const MeetingContextPanel = ({ item, onSave, readOnly }) => {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const text = typeof item.meetingContext === 'string' ? item.meetingContext : '';
  const hasContext = Boolean(text.trim());
  const canEdit = Boolean(onSave) && !readOnly;
  const tooLong = draft.length > MAX_CONTEXT_CHARS;

  if (!hasContext && !canEdit) return null;

  const beginEditing = () => {
    setDraft(text);
    setError('');
    setEditing(true);
  };

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      const result = await onSave(draft.trim() ? draft : '');
      if (result?.ok) setEditing(false);
      else setError(result?.message || 'The meeting context could not be saved.');
    } finally {
      setSaving(false);
    }
  };

  const actions = editing ? (
    <SpaceBetween size="xxs" direction="horizontal">
      <Button onClick={() => setEditing(false)} disabled={saving}>
        Cancel
      </Button>
      <Button variant="primary" onClick={save} loading={saving} disabled={tooLong}>
        Save context
      </Button>
    </SpaceBetween>
  ) : (
    canEdit && (
      <Button iconName="edit" onClick={beginEditing}>
        {hasContext ? 'Edit' : 'Add context'}
      </Button>
    )
  );

  return (
    <div data-testid="meeting-context-panel">
      <Container
        header={
          <Header
            variant="h4"
            description={hasContext ? formatUpdated(item.meetingContextUpdatedBy, item.meetingContextUpdatedAt) : null}
            actions={actions}
          >
            Meeting context
          </Header>
        }
      >
        <SpaceBetween size="s">
          {error && <StatusIndicator type="error">{error}</StatusIndicator>}
          {editing && (
            <SpaceBetween size="xs">
              <Textarea
                value={draft}
                onChange={({ detail }) => setDraft(detail.value)}
                rows={Math.min(20, Math.max(6, draft.split('\n').length + 1))}
                placeholder="Goals, background, terms, and what a good outcome looks like. Markdown."
                ariaLabel="Meeting context, Markdown"
                invalid={tooLong}
              />
              <Box variant="small" color={tooLong ? 'text-status-error' : 'text-body-secondary'}>
                {`${draft.length} of ${MAX_CONTEXT_CHARS} characters. For the assistant and the summary; not posted `}
                in the meeting chat. Saving an empty context clears it.
              </Box>
            </SpaceBetween>
          )}
          {!editing && hasContext && <SummaryMarkdown callId={item.callId}>{text}</SummaryMarkdown>}
          {!editing && !hasContext && <Box color="text-body-secondary">No meeting context.</Box>}
        </SpaceBetween>
      </Container>
    </div>
  );
};

export default MeetingContextPanel;
