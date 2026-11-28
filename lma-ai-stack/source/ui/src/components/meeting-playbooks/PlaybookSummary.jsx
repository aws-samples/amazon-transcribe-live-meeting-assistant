/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * What applying a playbook does, in words: the whiteboards it makes, the live
 * guidance checks it turns on, its quick actions and what it may cost. Shown
 * where a playbook is chosen (the meeting page, the Virtual Participant form)
 * and in the Skills page's preview. `playbook` is one entry of
 * meetingPlaybookOptions' Playbooks. See docs/meeting-playbooks.md.
 */
import React from 'react';
import { Box, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import { ASSERTIVENESS, costSentence } from './playbook-model';

/* eslint-disable react/prop-types */

const assertivenessLabel = (value) => ASSERTIVENESS.find((a) => a.value === value)?.label || 'Balanced';

const Section = ({ label, children }) => (
  <div>
    <Box variant="awsui-key-label">{label}</Box>
    <div>{children}</div>
  </div>
);

const PlaybookSummary = ({ playbook, compact = false }) => {
  if (!playbook) return null;
  const boards = playbook.Whiteboards || [];
  const guidance = playbook.Guidance;
  const checks = guidance?.checks || [];
  const actions = playbook.QuickActions || [];
  const unavailable = checks.filter((c) => !c.available);
  return (
    <SpaceBetween size={compact ? 'xs' : 's'}>
      {!compact && playbook.Description && <Box color="text-body-secondary">{playbook.Description}</Box>}
      <Section
        label={boards.length ? `Makes ${boards.length} whiteboard${boards.length === 1 ? '' : 's'}` : 'Whiteboards'}
      >
        {boards.length ? (
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            {boards.map((board) => (
              <li key={`${board.template}:${board.title}`}>
                <strong>{board.title}</strong> — {board.templateLabel}
                {board.assistantUpdates ? ', kept up to date by the assistant' : ', kept by hand'}
              </li>
            ))}
          </ul>
        ) : (
          <Box color="text-body-secondary">None</Box>
        )}
      </Section>
      <Section label="Live guidance">
        {checks.length ? (
          <span>
            {checks.map((c) => c.label).join(' · ')}{' '}
            <Box variant="span" color="text-body-secondary">
              ({assertivenessLabel(guidance.assertiveness)}
              {guidance.maxItemsPer10Min ? `, at most ${guidance.maxItemsPer10Min} items in 10 minutes` : ''}; no other
              check runs)
            </Box>
          </span>
        ) : (
          <Box color="text-body-secondary">Not turned on by this playbook</Box>
        )}
        {unavailable.length > 0 && (
          <Box variant="small" color="text-status-warning">
            {unavailable.map((c) => c.label).join(' and ')} {unavailable.length === 1 ? 'is' : 'are'} turned off for
            this deployment and will not run.
          </Box>
        )}
      </Section>
      <Section label="Quick actions in the assistant chat">
        {actions.length ? (
          <span>{actions.map((a) => a.label).join(' · ')}</span>
        ) : (
          <Box color="text-body-secondary">None</Box>
        )}
      </Section>
      {playbook.Estimate !== undefined && (
        <StatusIndicator type="info" colorOverride="grey">
          {costSentence(playbook.Estimate)}
        </StatusIndicator>
      )}
      {(playbook.Notes || []).length > 0 && (
        <Box variant="small" color="text-status-warning">
          {(playbook.Notes || []).join(' ')}
        </Box>
      )}
    </SpaceBetween>
  );
};

export default PlaybookSummary;
