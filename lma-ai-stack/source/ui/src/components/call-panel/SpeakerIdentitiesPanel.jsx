/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Name the voices in a meeting.
 *
 * The case this exists for is several people on one audio channel -- a
 * conference room microphone, or a meeting joined without a Virtual Participant.
 * Speaker separation tells those people apart but can only call them 'spk_0' and
 * 'spk_1', because nothing in the audio says who they are.
 *
 * Naming is per VOICE, never per segment: one name here relabels every segment
 * that voice ever produced, retroactively and going forward, because the name is
 * applied when a speaker is read rather than written onto the stored segments.
 * The row's turn count is shown for exactly that reason -- it is how many
 * segments a rename will move.
 *
 * Merging is here because splitting one person across two voice ids is the
 * common separation failure, and naming both rows 'Bob' without merging leaves
 * two Bobs that then confuse every summary.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Autosuggest,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  Container,
  ExpandableSection,
  Header,
  Input,
  Select,
  SpaceBetween,
  StatusIndicator,
  Textarea,
  TextContent,
} from '@cloudscape-design/components';

import { InfoLink } from '../common/info-link';
/* eslint-disable react/prop-types, react/destructuring-assignment */
import {
  buildSpeakerRoster,
  formatTalkTime,
  parseAttendeeList,
  parseSpeakerIdentities,
  serializeSpeakerIdentities,
} from '../common/speaker-identities';

const MERGE_PLACEHOLDER = { label: 'Same person as…', value: '' };

const SpeakerRow = ({ row, candidates, rosterRows, onRename, onMerge, onToggleLock, disabled }) => {
  // Local draft so typing is not fighting the subscription: an identity write
  // comes straight back through onUpdateCall, and re-deriving the input value
  // from that on every keystroke would move the caret.
  const [draft, setDraft] = useState(row.name);
  useEffect(() => {
    setDraft(row.name);
  }, [row.name]);

  const commit = () => {
    const trimmed = draft.trim();
    if (trimmed !== row.name) {
      onRename(row, trimmed);
    }
  };

  const mergeOptions = [
    MERGE_PLACEHOLDER,
    ...rosterRows
      .filter((other) => other.key !== row.key)
      .map((other) => ({
        label: other.name || `${other.defaultName} ${other.voiceId}`.trim(),
        value: other.key,
      })),
  ];

  const nameField = candidates.length ? (
    <Autosuggest
      value={draft}
      onChange={({ detail }) => setDraft(detail.value)}
      onBlur={commit}
      onSelect={({ detail }) => onRename(row, (detail.value || '').trim())}
      options={candidates.map((name) => ({ value: name }))}
      enteredTextLabel={(value) => `Use "${value}"`}
      placeholder={row.defaultName}
      disabled={disabled || row.locked}
      ariaLabel={`Name for voice ${row.voiceId || row.channel}`}
      empty="No matching attendee"
    />
  ) : (
    <Input
      value={draft}
      onChange={({ detail }) => setDraft(detail.value)}
      onBlur={commit}
      onKeyDown={({ detail }) => {
        if (detail.key === 'Enter') {
          commit();
        }
      }}
      placeholder={row.defaultName}
      disabled={disabled || row.locked}
      ariaLabel={`Name for voice ${row.voiceId || row.channel}`}
    />
  );

  return (
    <SpaceBetween direction="vertical" size="xxs">
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Box variant="small">
          {row.voiceId ? <Badge>{row.voiceId.replace(/[()]/g, '')}</Badge> : <Badge>{row.channel}</Badge>}
        </Box>
        <Box>{nameField}</Box>
        <Box variant="small" color="text-body-secondary">
          {`${row.turns} ${row.turns === 1 ? 'turn' : 'turns'} · ${formatTalkTime(row.talkTimeSeconds)}`}
        </Box>
        {row.status === 'proposed' && <StatusIndicator type="pending">suggested</StatusIndicator>}
        {row.mergedKeys.length > 0 && (
          <Box variant="small" color="text-body-secondary">
            {`+${row.mergedKeys.length} merged`}
          </Box>
        )}
        <Select
          selectedOption={MERGE_PLACEHOLDER}
          options={mergeOptions}
          onChange={({ detail }) => {
            if (detail.selectedOption.value) {
              onMerge(row, detail.selectedOption.value);
            }
          }}
          disabled={disabled || mergeOptions.length < 2}
          ariaLabel={`Merge voice ${row.voiceId || row.channel} into another speaker`}
        />
        <Button
          variant="icon"
          iconName={row.locked ? 'lock-private' : 'unlocked'}
          onClick={() => onToggleLock(row)}
          disabled={disabled || !row.name}
          ariaLabel={row.locked ? 'Unlock this name' : 'Lock this name'}
        />
      </SpaceBetween>
      {row.evidence?.quote && (
        <Box variant="small" color="text-body-secondary" padding={{ left: 'xl' }}>
          {`“${row.evidence.quote}”`}
        </Box>
      )}
    </SpaceBetween>
  );
};

const SpeakerIdentitiesPanel = ({ item, segments, setToolsOpen, onSave, readOnly }) => {
  const identities = useMemo(() => parseSpeakerIdentities(item.speakerIdentities), [item.speakerIdentities]);
  const roster = useMemo(
    () => buildSpeakerRoster(segments, identities, item.callerPhoneNumber),
    [segments, identities, item.callerPhoneNumber],
  );

  const [saving, setSaving] = useState(false);
  const [pasted, setPasted] = useState('');
  const [attendeesOpen, setAttendeesOpen] = useState(false);

  const namedCount = roster.filter((row) => row.name).length;

  const write = async (next) => {
    setSaving(true);
    try {
      await onSave(serializeSpeakerIdentities(next));
    } finally {
      setSaving(false);
    }
  };

  const handleRename = (row, name) => {
    const nextIdentities = { ...identities.identities };
    if (name) {
      nextIdentities[row.key] = {
        ...(nextIdentities[row.key] || {}),
        name,
        status: 'confirmed',
        source: 'manual',
        updatedAt: new Date().toISOString(),
      };
    } else {
      // Clearing the field removes the mapping, so the transcript falls back to
      // the voice id rather than showing an empty speaker.
      delete nextIdentities[row.key];
    }
    return write({ ...identities, identities: nextIdentities });
  };

  const handleMerge = (row, targetKey) =>
    write({ ...identities, merges: { ...identities.merges, [row.key]: targetKey } });

  const handleUnmergeAll = (row) => {
    const nextMerges = { ...identities.merges };
    row.mergedKeys.forEach((key) => delete nextMerges[key]);
    return write({ ...identities, merges: nextMerges });
  };

  const handleToggleLock = (row) => {
    const entry = identities.identities[row.key];
    if (!entry) {
      return undefined;
    }
    return write({
      ...identities,
      identities: { ...identities.identities, [row.key]: { ...entry, locked: !entry.locked } },
    });
  };

  const handleAddAttendees = () => {
    const parsed = parseAttendeeList(pasted);
    if (!parsed.length) {
      return undefined;
    }
    const merged = [...identities.candidates];
    parsed.forEach((name) => {
      if (!merged.some((existing) => existing.toLowerCase() === name.toLowerCase())) {
        merged.push(name);
      }
    });
    setPasted('');
    return write({ ...identities, candidates: merged });
  };

  const handleRemoveCandidate = (name) =>
    write({ ...identities, candidates: identities.candidates.filter((c) => c !== name) });

  const handleClearAll = () => write({ ...identities, identities: {}, merges: {}, segmentOverrides: {} });

  return (
    <Container
      header={
        <Header
          variant="h4"
          info={<InfoLink onFollow={() => setToolsOpen(true)} />}
          counter={roster.length ? `(${namedCount}/${roster.length})` : undefined}
          description="Name each voice once and the whole transcript updates, past segments included."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {saving && <StatusIndicator type="loading">Saving</StatusIndicator>}
              <ButtonDropdown
                items={[
                  { text: 'Paste attendee list', id: 'attendees', iconName: 'add-plus' },
                  {
                    text: 'Clear all names',
                    id: 'clear',
                    iconName: 'remove',
                    disabled: readOnly || (!Object.keys(identities.identities).length && !namedCount),
                  },
                ]}
                onItemClick={({ detail }) => {
                  if (detail.id === 'attendees') {
                    setAttendeesOpen(true);
                  } else if (detail.id === 'clear') {
                    handleClearAll();
                  }
                }}
                disabled={saving}
              >
                Actions
              </ButtonDropdown>
            </SpaceBetween>
          }
        >
          Speakers
        </Header>
      }
    >
      <SpaceBetween direction="vertical" size="s">
        {roster.length === 0 ? (
          <Box variant="p" color="text-body-secondary">
            No speakers yet. Voices appear here as soon as the transcript has final segments.
          </Box>
        ) : (
          roster.map((row) => (
            <SpaceBetween key={row.key} direction="vertical" size="xxs">
              <SpeakerRow
                row={row}
                candidates={identities.candidates}
                rosterRows={roster}
                onRename={handleRename}
                onMerge={handleMerge}
                onToggleLock={handleToggleLock}
                disabled={readOnly || saving}
              />
              {row.mergedKeys.length > 0 && !readOnly && (
                <Box padding={{ left: 'xl' }}>
                  <Button variant="inline-link" onClick={() => handleUnmergeAll(row)} disabled={saving}>
                    Separate merged voices
                  </Button>
                </Box>
              )}
            </SpaceBetween>
          ))
        )}

        <ExpandableSection
          headerText="Known attendees (optional)"
          expanded={attendeesOpen}
          onChange={({ detail }) => setAttendeesOpen(detail.expanded)}
          variant="footer"
        >
          <SpaceBetween direction="vertical" size="xs">
            <TextContent>
              <small>
                Paste the attendee list from a meeting invite, or any list of names, to pick from it above. Entirely
                optional — a meeting with no list works the same way, you just type the names.
              </small>
            </TextContent>
            {identities.candidates.length > 0 && (
              <SpaceBetween direction="horizontal" size="xxs">
                {identities.candidates.map((name) => (
                  <Button
                    key={name}
                    variant="inline-link"
                    iconName="close"
                    iconAlign="right"
                    onClick={() => handleRemoveCandidate(name)}
                    disabled={readOnly || saving}
                  >
                    {name}
                  </Button>
                ))}
              </SpaceBetween>
            )}
            <Textarea
              value={pasted}
              onChange={({ detail }) => setPasted(detail.value)}
              placeholder={'Bob Strahan <bob@example.com>\nSarah Lee (Organizer)\npriya.raman@example.com'}
              rows={3}
              disabled={readOnly || saving}
            />
            <Button onClick={handleAddAttendees} disabled={readOnly || saving || !pasted.trim()}>
              Add attendees
            </Button>
          </SpaceBetween>
        </ExpandableSection>
      </SpaceBetween>
    </Container>
  );
};

export default SpeakerIdentitiesPanel;
