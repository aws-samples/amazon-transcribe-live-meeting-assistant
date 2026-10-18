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
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Autosuggest,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  ExpandableSection,
  Input,
  Select,
  SpaceBetween,
  StatusIndicator,
  Textarea,
  TextContent,
  TokenGroup,
} from '@cloudscape-design/components';

import { InfoLink } from '../common/info-link';
/* eslint-disable react/prop-types, react/destructuring-assignment */
import {
  buildSpeakerRoster,
  formatTalkTime,
  hasMultipleVoicesOnAChannel,
  parseAttendeeList,
  parseSpeakerIdentities,
  serializeSpeakerIdentities,
} from '../common/speaker-identities';

const MERGE_PLACEHOLDER = { label: 'Same person as…', value: '' };

/**
 * How often to look for names while a meeting is running.
 *
 * Long enough that a meeting does not spend money on model calls that read
 * almost the same transcript twice, short enough that a name appears within a
 * minute or so of somebody introducing themselves. Introductions cluster in the
 * first minutes, which is also when the first pass lands.
 */
const LIVE_PASS_INTERVAL_MS = 45000;

/** How long the outcome of a pass stays in the panel header. */
const STATUS_VISIBLE_MS = 20000;

const SpeakerRow = ({
  row,
  candidates,
  rosterRows,
  onRename,
  onMerge,
  onToggleLock,
  onConfirm,
  onReject,
  disabled,
}) => {
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
      // Same channel only: the two channels are separate audio sources, and
      // folding the local microphone into a remote voice is never meant.
      .filter((other) => other.key !== row.key && other.channel === row.channel)
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
        {row.status === 'proposed' && (
          <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
            <StatusIndicator type="pending">suggested</StatusIndicator>
            <Button variant="inline-link" onClick={() => onConfirm(row)} disabled={disabled}>
              Confirm
            </Button>
            <Button variant="inline-link" onClick={() => onReject(row)} disabled={disabled}>
              Not {row.name}
            </Button>
          </SpaceBetween>
        )}
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

const SpeakerIdentitiesPanel = ({ item, segments, setToolsOpen, onSave, onIdentify, readOnly, isLive }) => {
  const identities = useMemo(() => parseSpeakerIdentities(item.speakerIdentities), [item.speakerIdentities]);
  const roster = useMemo(
    () => buildSpeakerRoster(segments, identities, item.callerPhoneNumber),
    [segments, identities, item.callerPhoneNumber],
  );

  const [saving, setSaving] = useState(false);
  const [pasted, setPasted] = useState('');
  const [attendeesOpen, setAttendeesOpen] = useState(false);
  const [identifying, setIdentifying] = useState(false);
  const [identifyStatus, setIdentifyStatus] = useState(null);
  // Read from inside the interval callback, which captures its closure once and
  // would never see a state update -- so a slow pass would overlap the next tick.
  const identifyingRef = useRef(false);
  // The interval must not depend on the identity of this callback. Holding it in
  // a ref means a caller that passes a fresh function every render -- which is
  // the default in React and was the actual bug -- cannot stop the timer
  // elapsing. The hook that supplies it memoises it as well; this is what makes
  // the panel correct on its own rather than only in combination.
  const identifyRef = useRef(onIdentify);
  identifyRef.current = onIdentify;
  const statusTimerRef = useRef(undefined);
  useEffect(() => () => clearTimeout(statusTimerRef.current), []);

  // A voice holding an unconfirmed SUGGESTION is still open: the backend treats
  // it that way too, specifically so a later self-introduction can upgrade a
  // guess. Counting it as named would stop the automatic passes exactly when
  // they still have something to find.
  const unnamedCount = roster.filter((row) => row.isPlaceholder && (!row.name || row.status === 'proposed')).length;

  /**
   * Run a naming pass and say what came back.
   *
   * The names themselves are not in the response -- the backend writes them, and
   * they arrive through the same subscription a rename by another user does. So
   * all this reports is the count, which is the one thing the user cannot see
   * for themselves when the answer is "none".
   */
  const runIdentify = async (force) => {
    const identify = identifyRef.current;
    if (!identify) {
      return;
    }
    setIdentifying(true);
    identifyingRef.current = true;
    setIdentifyStatus(null);
    try {
      const result = await identify({ force });
      if (!result?.Success) {
        setIdentifyStatus({ type: 'error', text: 'Could not identify speakers' });
      } else if (result.NamesApplied > 0) {
        setIdentifyStatus({
          type: 'success',
          text: `Identified ${result.NamesApplied} ${result.NamesApplied === 1 ? 'speaker' : 'speakers'}`,
        });
      } else {
        // The honest answer, rather than silence: the transcript so far simply
        // does not say who these people are.
        setIdentifyStatus({ type: 'info', text: 'Nothing in the transcript names them yet' });
      }
      // Cleared after a while so a stale "Identified 2 speakers" does not sit in
      // the header for the rest of the meeting.
      clearTimeout(statusTimerRef.current);
      statusTimerRef.current = setTimeout(() => setIdentifyStatus(null), STATUS_VISIBLE_MS);
    } finally {
      setIdentifying(false);
      identifyingRef.current = false;
    }
  };

  /**
   * While a meeting runs, look for names every so often.
   *
   * There is no backend scheduler: a pass happens when somebody is watching the
   * meeting, which is also when seeing the names appear is worth anything. The
   * end-of-meeting pass covers a meeting nobody had open. Passes stop once every
   * voice is named, and are not forced, so the backend's own "is there anything
   * here worth a model call" shortcut still applies.
   */
  useEffect(() => {
    if (!isLive || readOnly || unnamedCount === 0) {
      return undefined;
    }
    const timer = setInterval(() => {
      if (!identifyingRef.current) {
        runIdentify(false);
      }
    }, LIVE_PASS_INTERVAL_MS);
    return () => clearInterval(timer);
    // Keyed only on whether a pass is wanted at all. Neither runIdentify nor
    // onIdentify appears here on purpose: both change identity on every render,
    // so depending on either rebuilt the timer on every arriving transcript
    // segment and it only ever elapsed during silence -- precisely when there is
    // nothing new to read. Both are reached through refs instead.
  }, [isLive, readOnly, unnamedCount]);

  const namedCount = roster.filter((row) => row.name).length;

  // Starts open only on a channel separation actually split; once the user opens
  // or closes it themselves, their choice wins for the rest of the session.
  const [expandedOverride, setExpandedOverride] = useState(undefined);
  const expanded = expandedOverride ?? hasMultipleVoicesOnAChannel(roster);

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

  /**
   * Accept a suggested name as the user's own.
   *
   * Recorded as source 'manual', which is what stops a later pass revisiting it
   * -- a human has now looked at this and agreed.
   */
  const handleConfirm = (row) => {
    const entry = identities.identities[row.key];
    if (!entry) {
      return undefined;
    }
    return write({
      ...identities,
      identities: {
        ...identities.identities,
        [row.key]: { ...entry, status: 'confirmed', source: 'manual', updatedAt: new Date().toISOString() },
      },
    });
  };

  /**
   * Reject a suggested name.
   *
   * The entry is removed rather than blanked, so the voice reads as unnamed
   * again -- and remembered as rejected, so the next pass does not propose the
   * same name straight back.
   */
  const handleReject = (row) => {
    const nextIdentities = { ...identities.identities };
    delete nextIdentities[row.key];
    const rejected = { ...(identities.rejected || {}) };
    rejected[row.key] = [...new Set([...(rejected[row.key] || []), row.name])];
    return write({ ...identities, identities: nextIdentities, rejected });
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
    <ExpandableSection
      variant="container"
      expanded={expanded}
      onChange={({ detail }) => setExpandedOverride(detail.expanded)}
      headerText="Speakers"
      headerCounter={roster.length ? `(${namedCount}/${roster.length})` : undefined}
      headerDescription="Name each voice once and the whole transcript updates, past segments included."
      headerInfo={<InfoLink onFollow={() => setToolsOpen(true)} />}
      headerActions={
        <SpaceBetween direction="horizontal" size="xs">
          {saving && <StatusIndicator type="loading">Saving</StatusIndicator>}
          {!saving && identifyStatus && (
            // Announced, because the outcome of pressing the button -- especially
            // "nothing names them yet" -- is the whole feedback for that action.
            <Box role="status" ariaLive="polite">
              <StatusIndicator type={identifyStatus.type}>{identifyStatus.text}</StatusIndicator>
            </Box>
          )}
          {onIdentify && (
            <Button
              iconName="search"
              loading={identifying}
              disabled={readOnly || saving}
              onClick={() => runIdentify(true)}
            >
              Identify speakers
            </Button>
          )}
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
                onConfirm={handleConfirm}
                onReject={handleReject}
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
              <TokenGroup
                items={identities.candidates.map((name) => ({ label: name, dismissLabel: `Remove ${name}` }))}
                onDismiss={({ detail }) => handleRemoveCandidate(identities.candidates[detail.itemIndex])}
                readOnly={readOnly || saving}
              />
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
    </ExpandableSection>
  );
};

export default SpeakerIdentitiesPanel;
