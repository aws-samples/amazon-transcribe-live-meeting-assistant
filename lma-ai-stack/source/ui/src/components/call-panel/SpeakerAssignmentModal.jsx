/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Name a speaker from the Transcript tab, without going to the Details tab.
 *
 * Opened from a speaker's chip above the transcript, for the voice (or
 * voices) shown under that name. It does what the Speakers panel on the Details
 * tab does for one row, through the same identity map and the same write: name
 * the voice, which relabels every line it spoke; say it is the same person as
 * another voice on its channel (a merge); and separate voices merged into it
 * again. When two voices are shown under one name -- both named "Bob" without
 * being merged -- it can also combine them into one person.
 *
 * Nothing is written until Save, and then in one write. Who may save is
 * decided by the caller (`readOnly`), by the same rule as the Speakers panel,
 * and enforced again as a condition on the mutation.
 */
import React, { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Autosuggest,
  Box,
  Button,
  Checkbox,
  FormField,
  Input,
  Modal,
  Select,
  SpaceBetween,
} from '@cloudscape-design/components';

import { channelLabel } from '../common/channels';
import {
  formatTalkTime,
  serializeSpeakerIdentities,
  speakerRowLabel,
  withMerge,
  withoutMerges,
  withSpeakerName,
} from '../common/speaker-identities';

const NOT_MERGED = { label: 'No one else (a person of their own)', value: '' };

/** The stored speaker a voice key holds, to name a merged voice: `REMOTE#Speaker (spk_1)` is "Speaker (spk_1)". */
const voiceLabel = (key) => key.slice(key.indexOf('#') + 1) || key;

/**
 * The identity map after the modal's choices, applied in order: the voices
 * chosen to be separated, then the voices combined into the first, then a
 * merge into another person (which takes that person's name) or the new name.
 * Exported for its tests.
 */
export const applySpeakerAssignment = (identities, rows, { name, mergeInto, combine, separate }) => {
  let next = withoutMerges(identities, separate);
  const [primary, ...others] = rows;
  if (combine) {
    others.forEach((row) => {
      next = withMerge(next, row.key, primary.key);
    });
  }
  const voices = combine ? [primary] : rows;
  if (mergeInto) {
    voices.forEach((row) => {
      next = withMerge(next, row.key, mergeInto);
    });
    return next;
  }
  voices.forEach((row) => {
    if (name !== row.name) next = withSpeakerName(next, row.key, name);
  });
  return next;
};

const SpeakerAssignmentModal = ({ speaker, rows, roster, identities, onSave, onDismiss, readOnly = false }) => {
  const [primary] = rows;
  const [name, setName] = useState(primary?.name || '');
  const [mergeInto, setMergeInto] = useState('');
  const [combine, setCombine] = useState(false);
  const [separate, setSeparate] = useState([]);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

  // Opened for another speaker: start again from what that one holds.
  useEffect(() => {
    setName(primary?.name || '');
    setMergeInto('');
    setCombine(false);
    setSeparate([]);
    setFailed(false);
  }, [speaker]);

  const rowKeys = rows.map((row) => row.key);
  const channels = new Set(rows.map((row) => row.channel));
  // Voices are combined within a channel only: the two channels are separate
  // audio sources, and folding the local microphone into a remote voice is never meant.
  const canCombine = rows.length > 1 && channels.size === 1;
  const mergeOptions = useMemo(
    () => [
      NOT_MERGED,
      ...roster
        .filter((other) => !rowKeys.includes(other.key) && channels.has(other.channel))
        .map((other) => ({ label: speakerRowLabel(other), value: other.key })),
    ],
    [roster, rowKeys.join('|')],
  );
  const mergedKeys = rows.flatMap((row) => row.mergedKeys || []);
  const locked = rows.some((row) => row.locked);
  const trimmed = name.trim();

  const changed =
    Boolean(mergeInto) ||
    (combine && canCombine) ||
    separate.length > 0 ||
    rows.some((row) => trimmed !== (row.name || ''));

  if (!primary) return null;

  const save = async () => {
    setSaving(true);
    setFailed(false);
    try {
      const next = applySpeakerAssignment(identities, rows, {
        name: trimmed,
        mergeInto,
        combine: combine && canCombine,
        separate,
      });
      const saved = await onSave(serializeSpeakerIdentities(next));
      if (saved === false) setFailed(true);
      else onDismiss();
    } catch (error) {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  };

  const disabled = readOnly || saving;
  const nameDisabled = disabled || locked || Boolean(mergeInto);
  const nameField = identities.candidates?.length ? (
    <Autosuggest
      value={name}
      onChange={({ detail }) => setName(detail.value)}
      options={identities.candidates.map((candidate) => ({ value: candidate }))}
      enteredTextLabel={(value) => `Use "${value}"`}
      placeholder={primary.defaultName}
      disabled={nameDisabled}
      ariaLabel="Name"
      empty="No matching attendee"
    />
  ) : (
    <Input
      value={name}
      onChange={({ detail }) => setName(detail.value)}
      onKeyDown={({ detail }) => {
        if (detail.key === 'Enter' && changed && !saving) save();
      }}
      placeholder={primary.defaultName}
      disabled={nameDisabled}
      ariaLabel="Name"
    />
  );

  const turns = rows.reduce((total, row) => total + row.turns, 0);
  const talkTime = rows.reduce((total, row) => total + row.talkTimeSeconds, 0);

  let nameConstraint;
  if (locked) nameConstraint = 'This name is locked. Unlock it in the Speakers panel on the Details tab to change it.';
  else if (mergeInto) nameConstraint = 'Shown under the name of the person chosen below.';

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`Speaker: ${speaker}`}
      footer={
        <Box float="right">
          <SpaceBetween size="xs" direction="horizontal">
            <Button onClick={onDismiss} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={save} loading={saving} disabled={readOnly || !changed}>
              Save
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box variant="small" color="text-body-secondary">
          {`${channelLabel(primary.channel)} · ${turns} ${turns === 1 ? 'turn' : 'turns'} · ${formatTalkTime(
            talkTime,
          )}`}
        </Box>
        {readOnly && (
          <Alert type="info">
            Only the meeting&apos;s owner, an administrator or someone it is shared with can name speakers.
          </Alert>
        )}
        {failed && <Alert type="error">The speaker names could not be saved. Please try again.</Alert>}
        {rows.length > 1 && (
          <Alert type="info" header={`${rows.length} separate voices are shown as "${speaker}"`}>
            {canCombine ? (
              <Checkbox checked={combine} onChange={({ detail }) => setCombine(detail.checked)} disabled={disabled}>
                They are one person: combine them
              </Checkbox>
            ) : (
              'They are on different audio channels, so they stay separate. A new name applies to each.'
            )}
          </Alert>
        )}
        <FormField
          label="Name"
          description="Renames every line this voice spoke, earlier ones included."
          constraintText={nameConstraint}
        >
          {nameField}
        </FormField>
        {mergeOptions.length > 1 && (
          <FormField
            label="Same person as"
            description="When speaker separation split one person into two voices, combine them under one name."
          >
            <Select
              selectedOption={mergeOptions.find((option) => option.value === mergeInto) || NOT_MERGED}
              options={mergeOptions}
              onChange={({ detail }) => setMergeInto(detail.selectedOption.value)}
              disabled={disabled}
              ariaLabel="Same person as"
            />
          </FormField>
        )}
        {mergedKeys.length > 0 && (
          <FormField
            label="Combined voices"
            description="Voices combined into this person. Separate one to show it under its own name again."
          >
            <SpaceBetween size="xxs">
              {mergedKeys.map((key) => (
                <Checkbox
                  key={key}
                  checked={separate.includes(key)}
                  onChange={({ detail }) =>
                    setSeparate((previous) =>
                      detail.checked ? [...previous, key] : previous.filter((other) => other !== key),
                    )
                  }
                  disabled={disabled}
                >
                  {`Separate ${voiceLabel(key)}`}
                </Checkbox>
              ))}
            </SpaceBetween>
          </FormField>
        )}
      </SpaceBetween>
    </Modal>
  );
};

const ROW_SHAPE = PropTypes.shape({
  key: PropTypes.string.isRequired,
  channel: PropTypes.string,
  name: PropTypes.string,
  defaultName: PropTypes.string,
  voiceId: PropTypes.string,
  locked: PropTypes.bool,
  turns: PropTypes.number,
  talkTimeSeconds: PropTypes.number,
  mergedKeys: PropTypes.arrayOf(PropTypes.string),
});

SpeakerAssignmentModal.propTypes = {
  /** The name on the chip the modal was opened from. */
  speaker: PropTypes.string.isRequired,
  /** The roster rows of the voices shown under that name; usually one. */
  rows: PropTypes.arrayOf(ROW_SHAPE).isRequired,
  /** Every voice in the meeting, for "Same person as". */
  roster: PropTypes.arrayOf(ROW_SHAPE).isRequired,
  // eslint-disable-next-line react/forbid-prop-types
  identities: PropTypes.object.isRequired,
  /** Stores the serialized identity map; resolves to false when the write failed. */
  onSave: PropTypes.func.isRequired,
  onDismiss: PropTypes.func.isRequired,
  readOnly: PropTypes.bool,
};

export default SpeakerAssignmentModal;
