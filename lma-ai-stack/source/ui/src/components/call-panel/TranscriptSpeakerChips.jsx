/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The speaker chips above the transcript, grouped by platform name.
 *
 * A chip limits the transcript to one speaker. Where a platform name carries
 * several voice labels -- 'Bob Strahan (spk_0)', 'Bob Strahan (spk_1)' -- its
 * chips sit together with a one-click toggle:
 *
 *  - One person: every line under the name shows the name alone, and the chip's
 *    edit button opens the speaker modal (rename, combine, separate).
 *  - Several people: each voice has a chip of its own -- 'ConfRm_1 · Voice 1' --
 *    renamed in place, which renames every line that voice spoke.
 *
 * The toggle and the renames are written to the meeting's speaker identity map
 * (speaker-modes.js, speaker-identities.js), so every reader -- the downloads,
 * the summary, the meeting assistant -- shows the same names. Who may change
 * them is decided by the caller, by the same rule as the Speakers panel.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Input, SegmentedControl, ToggleButton } from '@cloudscape-design/components';

import { describeSpeakerMode, ONE_PERSON, SEVERAL_PEOPLE } from '../common/speaker-modes';

const MODE_OPTIONS = [
  { id: ONE_PERSON, text: 'One person' },
  { id: SEVERAL_PEOPLE, text: 'Several people' },
];

/** The chip itself: a filter toggle when there is more than one speaker, else the name. */
const ChipLabel = ({ name, filterable, selected, onToggle }) =>
  filterable ? (
    <ToggleButton pressed={selected} onChange={({ detail }) => onToggle(name, detail.pressed)}>
      {name}
    </ToggleButton>
  ) : (
    <Box variant="span" fontSize="body-s">
      {name}
    </Box>
  );

ChipLabel.propTypes = {
  name: PropTypes.string.isRequired,
  filterable: PropTypes.bool.isRequired,
  selected: PropTypes.bool.isRequired,
  onToggle: PropTypes.func.isRequired,
};

/** One voice of a name shown as several people, renamed in place. */
const VoiceChip = ({ chip, filterable, selected, onToggle, onRename = undefined, onMore = undefined }) => {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(chip.name);
  useEffect(() => {
    if (!editing) setDraft(chip.name);
  }, [chip.name, editing]);

  const commit = () => {
    setEditing(false);
    const trimmed = draft.trim();
    if (trimmed !== chip.name) onRename(chip.keys, trimmed);
  };

  if (editing) {
    return (
      <span className="transcript-speaker-chip transcript-speaker-chip-editing">
        <Input
          value={draft}
          autoFocus
          ariaLabel={`Name for ${chip.name}`}
          placeholder="Name"
          onChange={({ detail }) => setDraft(detail.value)}
          onBlur={commit}
          onKeyDown={({ detail }) => {
            if (detail.key === 'Enter') commit();
            if (detail.key === 'Escape') {
              setDraft(chip.name);
              setEditing(false);
            }
          }}
        />
        {onMore && (
          <Button variant="inline-link" onClick={() => onMore(chip.name)}>
            More options
          </Button>
        )}
      </span>
    );
  }
  return (
    <span className="transcript-speaker-chip">
      <ChipLabel name={chip.name} filterable={filterable} selected={selected} onToggle={onToggle} />
      {onRename && (
        <Button variant="icon" iconName="edit" ariaLabel={`Rename ${chip.name}`} onClick={() => setEditing(true)} />
      )}
    </span>
  );
};

const CHIP_SHAPE = PropTypes.shape({ name: PropTypes.string, keys: PropTypes.arrayOf(PropTypes.string) });

VoiceChip.propTypes = {
  chip: CHIP_SHAPE.isRequired,
  filterable: PropTypes.bool.isRequired,
  selected: PropTypes.bool.isRequired,
  onToggle: PropTypes.func.isRequired,
  onRename: PropTypes.func,
  onMore: PropTypes.func,
};

const TranscriptSpeakerChips = ({
  entries,
  filterable,
  selectedSpeakers,
  onToggleSpeaker,
  editableSpeakers = null,
  onEditSpeaker = undefined,
  onSetMode = undefined,
  onRenameVoices = undefined,
}) => {
  const editButton = (name) =>
    onEditSpeaker && editableSpeakers?.has(name) ? (
      <Button
        variant="icon"
        iconName="edit"
        ariaLabel={`Name or combine ${name}`}
        onClick={() => onEditSpeaker(name)}
      />
    ) : null;
  const chipFor = (name) => (
    <ChipLabel
      name={name}
      filterable={filterable}
      selected={selectedSpeakers.includes(name)}
      onToggle={onToggleSpeaker}
    />
  );

  return entries.map((entry) => {
    if (entry.type !== 'group') {
      return (
        <span key={`name:${entry.name}`} className="transcript-speaker-chip">
          {chipFor(entry.name)}
          {/* Rename, combine or separate this speaker's voices, here rather
              than on the Details tab. */}
          {editButton(entry.name)}
        </span>
      );
    }
    const several = entry.mode === SEVERAL_PEOPLE;
    return (
      <span
        key={`group:${entry.key}`}
        className="transcript-speaker-group"
        role="group"
        aria-label={`Voices under ${entry.base}`}
        data-testid="transcript-speaker-group"
      >
        {several && (
          <Box variant="span" fontSize="body-s" color="text-body-secondary">
            {`${entry.base}:`}
          </Box>
        )}
        {several
          ? entry.chips.map((chip) => (
              <VoiceChip
                key={chip.name}
                chip={chip}
                filterable={filterable}
                selected={selectedSpeakers.includes(chip.name)}
                onToggle={onToggleSpeaker}
                onRename={onRenameVoices}
                onMore={onEditSpeaker && editableSpeakers?.has(chip.name) ? onEditSpeaker : undefined}
              />
            ))
          : entry.chips.map((chip) => (
              <span key={chip.name} className="transcript-speaker-chip">
                {chipFor(chip.name)}
                {editButton(chip.name)}
              </span>
            ))}
        <span title={describeSpeakerMode(entry.group)} className="transcript-speaker-mode">
          <SegmentedControl
            label={`How the voices under ${entry.base} are shown`}
            selectedId={entry.mode}
            options={MODE_OPTIONS.map((option) => ({ ...option, disabled: !onSetMode }))}
            onChange={({ detail }) => onSetMode?.(entry.key, detail.selectedId)}
          />
        </span>
      </span>
    );
  });
};

TranscriptSpeakerChips.propTypes = {
  /** speakerChipLayout's entries, in order. */
  entries: PropTypes.arrayOf(
    PropTypes.shape({
      type: PropTypes.oneOf(['group', 'name']).isRequired,
      key: PropTypes.string,
      base: PropTypes.string,
      mode: PropTypes.string,
      name: PropTypes.string,
      chips: PropTypes.arrayOf(CHIP_SHAPE),
    }),
  ).isRequired,
  /** Whether the chips filter the transcript (there is more than one speaker). */
  filterable: PropTypes.bool.isRequired,
  selectedSpeakers: PropTypes.arrayOf(PropTypes.string).isRequired,
  onToggleSpeaker: PropTypes.func.isRequired,
  /** Names whose voices the speaker modal can edit; null when nothing may be edited. */
  // eslint-disable-next-line react/forbid-prop-types
  editableSpeakers: PropTypes.object,
  /** Opens the speaker modal for a name. */
  onEditSpeaker: PropTypes.func,
  /** Shows a group's voices as one person or several; absent when the reader may not. */
  onSetMode: PropTypes.func,
  /** Names the voices behind one chip; absent when the reader may not. */
  onRenameVoices: PropTypes.func,
};

export default TranscriptSpeakerChips;
