/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Choosing a meeting playbook: every playbook, the suggested ones marked with
 * why, what the selected one does and costs, and, when the meeting already runs
 * one, whether to delete the whiteboards it made that only the assistant has written on.
 * Nothing is applied until Apply. See docs/meeting-playbooks.md.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Modal,
  SpaceBetween,
  Spinner,
  Tiles,
} from '@cloudscape-design/components';
import PlaybookSummary from './PlaybookSummary';
import { TAKES_EFFECT } from './playbook-model';

/* eslint-disable react/prop-types */

/** A tile's second line: what the playbook sets up, in a few words. */
export const outline = (playbook) => {
  const parts = [];
  const boards = (playbook?.Whiteboards || []).length;
  const checks = (playbook?.Guidance?.checks || []).length;
  const actions = (playbook?.QuickActions || []).length;
  if (boards) parts.push(`${boards} whiteboard${boards === 1 ? '' : 's'}`);
  if (checks) parts.push(`${checks} guidance check${checks === 1 ? '' : 's'}`);
  if (actions) parts.push(`${actions} quick action${actions === 1 ? '' : 's'}`);
  return parts.join(' · ') || undefined;
};

const ChoosePlaybookModal = ({
  visible,
  options,
  loading = false,
  error = '',
  currentSkillId = '',
  initialSkillId = '',
  onApply,
  onDismiss,
}) => {
  const playbooks = options?.Playbooks || [];
  const suggestions = useMemo(
    () => Object.fromEntries((options?.Suggestions || []).map((s) => [s.SkillId, s])),
    [options],
  );
  const firstChoice =
    initialSkillId ||
    (options?.Suggestions || []).find((s) => s.SkillId !== currentSkillId)?.SkillId ||
    playbooks.find((p) => p.SkillId !== currentSkillId)?.SkillId ||
    '';
  const [selected, setSelected] = useState(firstChoice);
  const [deleteBoards, setDeleteBoards] = useState(false);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState('');

  useEffect(() => {
    if (visible) {
      setSelected(firstChoice);
      setDeleteBoards(false);
      setFailure('');
    }
    // Reset each time the dialog opens, not on every new options object.
  }, [visible]);
  useEffect(() => {
    if (visible && !selected && firstChoice) setSelected(firstChoice);
  }, [visible, selected, firstChoice]);

  // The suggested ones first, best first, then the rest as listed.
  const ordered = [
    ...(options?.Suggestions || []).map((s) => playbooks.find((p) => p.SkillId === s.SkillId)).filter(Boolean),
    ...playbooks.filter((p) => !suggestions[p.SkillId]),
  ];
  const chosen = playbooks.find((p) => p.SkillId === selected);
  const current = playbooks.find((p) => p.SkillId === currentSkillId);
  const switching = Boolean(currentSkillId) && selected !== currentSkillId;

  const apply = async () => {
    setSaving(true);
    setFailure('');
    try {
      const result = await onApply(selected, { deleteWhiteboards: switching && deleteBoards });
      if (result?.ok) onDismiss();
      else setFailure(result?.message || 'The playbook could not be applied.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header={currentSkillId ? 'Change the meeting playbook' : 'Choose a meeting playbook'}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={apply}
              loading={saving}
              disabled={!chosen || selected === currentSkillId}
              data-testid="apply-playbook"
            >
              {chosen ? `Apply ${chosen.DisplayName}` : 'Apply'}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {loading && <Spinner />}
        {error && <Alert type="error">{error}</Alert>}
        {!loading && !error && playbooks.length === 0 && (
          <Alert type="info">
            There are no playbooks yet. An Admin makes one on the Skills page, as a skill with a playbook section.
          </Alert>
        )}
        {playbooks.length > 0 && (
          <Tiles
            ariaLabel="Playbooks"
            columns={3}
            value={selected}
            onChange={({ detail }) => setSelected(detail.value)}
            items={ordered.map((p) => ({
              value: p.SkillId,
              label: (
                <SpaceBetween direction="horizontal" size="xxs">
                  <span>{p.DisplayName}</span>
                  {p.SkillId === currentSkillId && <Badge>Current</Badge>}
                  {suggestions[p.SkillId] && p.SkillId !== currentSkillId && <Badge color="blue">Suggested</Badge>}
                </SpaceBetween>
              ),
              description: suggestions[p.SkillId]?.Reason || outline(p),
            }))}
          />
        )}
        {chosen && (
          <Box padding={{ top: 'xs' }}>
            <PlaybookSummary playbook={chosen} />
          </Box>
        )}
        {chosen && selected !== currentSkillId && (
          <Box variant="small" color="text-body-secondary">
            {TAKES_EFFECT} Applying it turns on only what it names; removing it later puts back what was on before.
          </Box>
        )}
        {switching && (
          <Checkbox checked={deleteBoards} onChange={({ detail }) => setDeleteBoards(detail.checked)}>
            Delete the whiteboards {current?.DisplayName || 'the current playbook'} made that only the assistant has written on (what it wrote on them is lost)
          </Checkbox>
        )}
        {failure && <Alert type="error">{failure}</Alert>}
      </SpaceBetween>
    </Modal>
  );
};

export default ChoosePlaybookModal;
