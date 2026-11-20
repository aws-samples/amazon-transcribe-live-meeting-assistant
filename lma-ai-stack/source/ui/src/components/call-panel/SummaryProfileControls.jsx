/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting's summary profile on the meeting page: which profile its summary
 * is written with when it ends, and the choice Regenerate offers. See
 * docs/transcript-summarization.md#summary-profiles.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { Alert, Box, Button, FormField, Modal, Select, SpaceBetween } from '@cloudscape-design/components';

import { summaryProfileOptions } from '../../hooks/use-summary-profiles';
import { optionFor } from '../common/meeting-preparation';

const PROFILE = PropTypes.shape({ Name: PropTypes.string, Label: PropTypes.string, Description: PropTypes.string });

/** A profile's label, from the listed profiles; its name when it is not listed. */
export const profileLabel = (profiles, name) => {
  const wanted = name || 'default';
  const found = (profiles || []).find((p) => p.Name === wanted);
  return found?.Label || (wanted === 'default' ? 'Default' : wanted);
};

/** The meeting's summary profile, and an editor for it. */
export const SummaryProfileSetting = ({
  value = null,
  profiles = undefined,
  onChange = undefined,
  readOnly = false,
}) => {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('default');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const options = summaryProfileOptions(profiles, value);
  const begin = () => {
    setDraft(value || 'default');
    setError('');
    setEditing(true);
  };
  const save = async () => {
    setSaving(true);
    const result = await onChange(draft);
    setSaving(false);
    if (result?.ok) setEditing(false);
    else setError(result?.message || 'The summary profile could not be saved.');
  };
  return (
    <SpaceBetween size="xxs" direction="horizontal" alignItems="center">
      <Box variant="small">{`Summary profile: ${profileLabel(profiles, value)}`}</Box>
      {onChange && !readOnly && (
        <Button variant="inline-icon" iconName="edit" ariaLabel="Change the summary profile" onClick={begin} />
      )}
      {editing && (
        <Modal
          visible
          onDismiss={() => setEditing(false)}
          header="Summary profile"
          footer={
            <Box float="right">
              <SpaceBetween size="xs" direction="horizontal">
                <Button onClick={() => setEditing(false)} disabled={saving}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={save} loading={saving}>
                  Save
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            {error && <Alert type="error">{error}</Alert>}
            <FormField
              label="Summary profile"
              description="The sections of the summary written when the meeting ends, and by a later Regenerate."
            >
              <Select
                selectedOption={optionFor(options, draft)}
                options={options}
                onChange={({ detail }) => setDraft(detail.selectedOption.value)}
                ariaLabel="Summary profile"
              />
            </FormField>
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
};

SummaryProfileSetting.propTypes = {
  value: PropTypes.string,
  profiles: PropTypes.arrayOf(PROFILE),
  onChange: PropTypes.func,
  readOnly: PropTypes.bool,
};

/** Regenerate, with the profile to write the new summary with. */
export const RegenerateDialog = ({
  profiles = undefined,
  initial = 'default',
  handEdited = false,
  onCancel,
  onConfirm,
}) => {
  const options = summaryProfileOptions(profiles, initial);
  const [choice, setChoice] = useState(initial || 'default');
  return (
    <Modal
      visible
      onDismiss={onCancel}
      header={handEdited ? 'Replace the edited summary?' : 'Regenerate the summary'}
      footer={
        <Box float="right">
          <SpaceBetween size="xs" direction="horizontal">
            <Button onClick={onCancel}>Cancel</Button>
            <Button variant="primary" onClick={() => onConfirm(choice)}>
              Regenerate
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {handEdited && (
          <Box>
            This summary was edited by hand. Regenerating replaces it with a newly generated one, and the edited text
            cannot be recovered.
          </Box>
        )}
        <FormField
          label="Summary profile"
          description="The sections to write this summary with. The meeting's own summary profile is not changed."
        >
          <Select
            selectedOption={optionFor(options, choice)}
            options={options}
            onChange={({ detail }) => setChoice(detail.selectedOption.value)}
            ariaLabel="Summary profile for this summary"
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

RegenerateDialog.propTypes = {
  profiles: PropTypes.arrayOf(PROFILE),
  initial: PropTypes.string,
  handEdited: PropTypes.bool,
  onCancel: PropTypes.func.isRequired,
  onConfirm: PropTypes.func.isRequired,
};
