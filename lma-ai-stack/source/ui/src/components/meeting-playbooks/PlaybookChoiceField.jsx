/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The Virtual Participant form's meeting playbook: the profile's default, none,
 * or a playbook; a suggestion from the meeting's name and agenda, offered with
 * a button and never chosen by itself; and what the chosen one does and costs.
 * See docs/meeting-playbooks.md.
 */
import React from 'react';
import { Alert, Button, ExpandableSection, FormField, Select, SpaceBetween } from '@cloudscape-design/components';
import PlaybookSummary from './PlaybookSummary';
import { usePlaybookOptions } from './playbook-api';
import { DEFAULT_CHOICE, NO_PLAYBOOK, playbookSelectOptions } from './playbook-choice';

/* eslint-disable react/prop-types */

const DESCRIPTION =
  'Sets the meeting up when it starts: whiteboards, live guidance checks and quick actions in the assistant ' +
  'chat. You can change it on the meeting page while the meeting runs.';

const PlaybookChoiceField = ({ choice, onChange, meetingName = '', agenda = '', disabled = false, client }) => {
  const { loading, error, options } = usePlaybookOptions({ title: meetingName, agenda, client });
  const selectOptions = playbookSelectOptions(options);
  const value = choice || DEFAULT_CHOICE;
  // A stored choice of a playbook deleted since is still shown, so it can be changed.
  const selected = selectOptions.find((o) => o.value === value) || {
    value,
    label: `${value} (no longer exists; the meeting will run without a playbook)`,
  };
  let effective = value === NO_PLAYBOOK ? null : value;
  if (value === DEFAULT_CHOICE) effective = options.ProfileDefault;
  const described = (options.Playbooks || []).find((p) => p.SkillId === effective);
  const suggestion = (options.Suggestions || []).find((s) => s.SkillId !== effective);

  return (
    <FormField label="Meeting playbook" description={DESCRIPTION} errorText={error || undefined} stretch>
      <SpaceBetween size="xs">
        <Select
          selectedOption={selected}
          onChange={({ detail }) => onChange(detail.selectedOption.value)}
          options={selectOptions}
          statusType={loading ? 'loading' : 'finished'}
          ariaLabel="Meeting playbook"
          disabled={disabled}
        />
        {suggestion && !disabled && (
          <Alert
            type="info"
            header={`Suggested: ${suggestion.DisplayName}`}
            action={
              <Button onClick={() => onChange(suggestion.SkillId)} data-testid="use-suggested-playbook">
                Use it
              </Button>
            }
          >
            {suggestion.Reason}
          </Alert>
        )}
        {described && (
          <ExpandableSection headerText={`What ${described.DisplayName} does`} variant="footer">
            <PlaybookSummary playbook={described} compact />
          </ExpandableSection>
        )}
      </SpaceBetween>
    </FormField>
  );
};

export default PlaybookChoiceField;
