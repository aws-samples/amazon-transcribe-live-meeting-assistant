/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting's playbook on the meeting page: which one it runs and what that
 * turned on, a suggestion from the meeting's name and agenda when it runs none
 * (shown, never applied by itself), and, for the meeting's owner and Admins,
 * choosing, switching and removing one. Not shown to Participants. A component
 * of its own so the Live panel can place it. See docs/meeting-playbooks.md.
 */
import React, { useState } from 'react';
import { Alert, Box, Button, Checkbox, Container, Header, Modal, SpaceBetween } from '@cloudscape-design/components';
import ChoosePlaybookModal from './ChoosePlaybookModal';
import { usePlaybookOptions } from './playbook-api';
import { checkLabel, meetingPlaybook } from './playbook-model';

/* eslint-disable react/prop-types */

const ORIGINS = {
  chosen: 'chosen for this meeting',
  'virtual-participant': 'chosen when the Virtual Participant was set up',
  'profile-default': "the configuration profile's default",
};

const DESCRIPTION = 'Sets the meeting up: whiteboards, live guidance checks and quick actions in the assistant chat.';

const timeOf = (value) => {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};

/** One line saying what the applied playbook turned on. */
export const appliedSummary = (record) => {
  const parts = [];
  const boards = record?.Whiteboards || [];
  if (boards.length)
    parts.push(
      `${boards.length} whiteboard${boards.length === 1 ? '' : 's'} (${boards.map((b) => b.Title).join(', ')})`,
    );
  const checks = record?.Guidance?.Checks || [];
  if (checks.length) parts.push(`live guidance: ${checks.map(checkLabel).join(', ')}`);
  const actions = record?.QuickActions || [];
  if (actions.length)
    parts.push(`${actions.length} quick action${actions.length === 1 ? '' : 's'} in the assistant chat`);
  return parts.length ? `Turned on ${parts.join('; ')}.` : 'It turns nothing on.';
};

const MeetingPlaybookPanel = ({ item, canChoose = false, isLive = false, onApply, client }) => {
  const record = meetingPlaybook(item);
  const applied = record?.SkillId ? record : null;
  const [choosing, setChoosing] = useState(false);
  const [initial, setInitial] = useState('');
  const [removing, setRemoving] = useState(false);
  const [deleteBoards, setDeleteBoards] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const ended = !isLive;
  const { loading, error, options } = usePlaybookOptions({
    callId: item?.callId,
    enabled: Boolean(item?.callId) && canChoose,
    refreshKey: item?.playbookUpdatedAt,
    client,
  });

  if (!canChoose && !record) return null;

  const apply = async (skillId, { deleteWhiteboards = false } = {}) => {
    const result = await onApply({ SkillId: skillId || null, DeleteWhiteboards: deleteWhiteboards });
    if (result?.ok) {
      setNotice(
        skillId
          ? { type: 'success', text: 'Playbook applied. Its whiteboards are on the Whiteboards tab.' }
          : { type: 'success', text: 'Playbook removed; what was on before it is back.' },
      );
    }
    return result;
  };

  const remove = async () => {
    setBusy(true);
    try {
      const result = await apply(null, { deleteWhiteboards: deleteBoards });
      if (!result?.ok) setNotice({ type: 'error', text: result?.message || 'The playbook could not be removed.' });
      setRemoving(false);
    } finally {
      setBusy(false);
    }
  };

  const suggestion = !applied && canChoose ? (options.Suggestions || [])[0] : null;
  const open = (skillId = '') => {
    setInitial(skillId);
    setChoosing(true);
  };

  return (
    <Container
      data-testid="meeting-playbook-panel"
      header={
        <Header
          variant="h2"
          description={DESCRIPTION}
          actions={
            canChoose ? (
              <SpaceBetween direction="horizontal" size="xs">
                {applied && (
                  <Button onClick={() => setRemoving(true)} data-testid="remove-playbook">
                    Remove
                  </Button>
                )}
                <Button onClick={() => open()} disabled={ended} data-testid="choose-playbook">
                  {applied ? 'Change' : 'Choose a playbook'}
                </Button>
              </SpaceBetween>
            ) : undefined
          }
        >
          Meeting playbook
        </Header>
      }
    >
      <SpaceBetween size="s">
        {applied ? (
          <div>
            <Box variant="p">
              <strong>{applied.DisplayName || applied.Name}</strong>, {ORIGINS[applied.Origin] || 'applied'}
              {applied.AppliedAt ? ` at ${timeOf(applied.AppliedAt)}` : ''}.
            </Box>
            <Box variant="small" color="text-body-secondary">
              {appliedSummary(applied)}
            </Box>
          </div>
        ) : (
          <Box color="text-body-secondary">No playbook. The meeting runs with the deployment&apos;s settings.</Box>
        )}
        {(record?.Notes || []).length > 0 && (
          <Alert type={applied ? 'info' : 'warning'}>{record.Notes.join(' ')}</Alert>
        )}
        {suggestion && !ended && (
          <Alert
            type="info"
            header={`Suggested: ${suggestion.DisplayName}`}
            action={<Button onClick={() => open(suggestion.SkillId)}>Review</Button>}
          >
            {suggestion.Reason} Nothing changes until you apply it.
          </Alert>
        )}
        {canChoose && ended && !applied && (
          <Box variant="small" color="text-body-secondary">
            A playbook applies to a meeting in progress.
          </Box>
        )}
        {notice && (
          <Alert type={notice.type} dismissible onDismiss={() => setNotice(null)}>
            {notice.text}
          </Alert>
        )}
      </SpaceBetween>
      {canChoose && (
        <ChoosePlaybookModal
          visible={choosing}
          options={options}
          loading={loading}
          error={error}
          currentSkillId={applied?.SkillId || ''}
          initialSkillId={initial}
          onApply={apply}
          onDismiss={() => setChoosing(false)}
        />
      )}
      <Modal
        visible={removing}
        onDismiss={() => setRemoving(false)}
        header="Remove the meeting playbook"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setRemoving(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={remove} loading={busy} data-testid="confirm-remove-playbook">
                Remove
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            Live guidance and the whiteboard updates go back to how they were before the playbook, and its quick actions
            leave the assistant chat. Its whiteboards are kept unless you choose otherwise.
          </Box>
          <Checkbox checked={deleteBoards} onChange={({ detail }) => setDeleteBoards(detail.checked)}>
            Also delete the whiteboards it made that only the assistant has written on (what the assistant wrote on them is lost)
          </Checkbox>
        </SpaceBetween>
      </Modal>
    </Container>
  );
};

export default MeetingPlaybookPanel;
