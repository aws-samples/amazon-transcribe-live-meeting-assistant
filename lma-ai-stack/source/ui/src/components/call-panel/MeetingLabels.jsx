/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting's labels (key=value tags another system finds its meetings by)
 * as chips, and its audience (internal, external or unspecified), with an
 * editor for those who can change the meeting. Hidden when the meeting has
 * neither and the viewer cannot add them. See
 * docs/meeting-context-and-checklist.md.
 */
import React, { useState } from 'react';
import {
  Badge,
  Box,
  Button,
  FormField,
  Select,
  SpaceBetween,
  StatusIndicator,
  Textarea,
} from '@cloudscape-design/components';

import {
  AUDIENCE_OPTIONS,
  audienceLabel,
  labelsToText,
  optionFor,
  parseLabelsText,
} from '../common/meeting-preparation';

/* eslint-disable react/prop-types */

const MeetingLabels = ({ item, onSaveLabels, onSaveAudience, readOnly }) => {
  const labels = Array.isArray(item.labels) ? item.labels.filter(Boolean) : [];
  const audience = item.audience || '';
  const canEdit = Boolean(onSaveLabels && onSaveAudience) && !readOnly;
  const [editing, setEditing] = useState(false);
  const [labelsText, setLabelsText] = useState('');
  const [audienceDraft, setAudienceDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  if (!labels.length && !audience && !canEdit) return null;

  const parsed = editing ? parseLabelsText(labelsText) : { labels: [], error: null };

  const beginEditing = () => {
    setLabelsText(labelsToText(labels));
    setAudienceDraft(audience);
    setError('');
    setEditing(true);
  };

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      const results = [];
      if (labelsToText(parsed.labels) !== labelsToText(labels)) results.push(await onSaveLabels(parsed.labels));
      if (audienceDraft !== audience) results.push(await onSaveAudience(audienceDraft || null));
      const failed = results.find((result) => !result?.ok);
      if (failed) setError(failed.message || 'The change could not be saved.');
      else setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <Box data-testid="meeting-labels-editor">
        <SpaceBetween size="s">
          <FormField
            label="Labels"
            description="One key=value per line, for example project=website-relaunch. Keys are lower case."
            errorText={parsed.error}
          >
            <Textarea
              value={labelsText}
              onChange={({ detail }) => setLabelsText(detail.value)}
              rows={Math.min(8, Math.max(3, labelsText.split('\n').length + 1))}
              ariaLabel="Labels, one key=value per line"
            />
          </FormField>
          <FormField label="Audience" description="Whether people outside your organization are in the meeting.">
            <Select
              selectedOption={optionFor(AUDIENCE_OPTIONS, audienceDraft)}
              options={AUDIENCE_OPTIONS}
              onChange={({ detail }) => setAudienceDraft(detail.selectedOption.value)}
              ariaLabel="Audience"
            />
          </FormField>
          {error && <StatusIndicator type="error">{error}</StatusIndicator>}
          <SpaceBetween size="xxs" direction="horizontal">
            <Button onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={save} loading={saving} disabled={Boolean(parsed.error)}>
              Save
            </Button>
          </SpaceBetween>
        </SpaceBetween>
      </Box>
    );
  }

  return (
    <Box data-testid="meeting-labels">
      <SpaceBetween size="xs" direction="horizontal" alignItems="center">
        <Box variant="awsui-key-label">Audience</Box>
        <Badge color={audience === 'external' ? 'blue' : 'grey'}>{audienceLabel(audience)}</Badge>
        <Box variant="awsui-key-label">Labels</Box>
        {labels.length ? (
          labels.map(({ Key, Value }) => (
            <Badge key={Key} color="grey">
              {Value ? `${Key}=${Value}` : Key}
            </Badge>
          ))
        ) : (
          <Box color="text-body-secondary">None</Box>
        )}
        {canEdit && (
          <Button variant="inline-icon" iconName="edit" ariaLabel="Edit labels and audience" onClick={beginEditing} />
        )}
      </SpaceBetween>
    </Box>
  );
};

export default MeetingLabels;
