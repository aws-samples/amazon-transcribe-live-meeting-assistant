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
 *
 * The voices of a platform name that can be shown as one person or several
 * (speaker-modes.js) are listed together under that name, with its
 * one-person / several-people icon toggle, and each row reads as its lines do: 'Voice 2' and the
 * placeholder 'ConfRm_1 · Voice 2' while unnamed. Under a name shown as one
 * person, naming one of its voices names them all, because the transcript shows
 * that one name for every line under it.
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

import { channelLabel } from '../common/channels';
import { InfoLink } from '../common/info-link';
/* eslint-disable react/prop-types, react/destructuring-assignment */
import {
  buildSpeakerRoster,
  formatTalkTime,
  hasMultipleVoicesOnAChannel,
  nameableVoiceKeys,
  parseAttendeeList,
  parseSpeakerIdentities,
  serializeSpeakerIdentities,
  speakerDisplayRows,
  speakerGroups,
  speakerRowLabel,
  withMerge,
  withoutMerges,
  withSpeakerName,
} from '../common/speaker-identities';
import { offersModeChoice, ONE_PERSON, withSpeakerMode } from '../common/speaker-modes';
import { own } from '../common/speaker-keys';
import SpeakerModeToggle from './SpeakerModeToggle';

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

/**
 * The resolver's answers that end the automatic passes (see `Reason` in
 * resolveSpeakerNames): nothing is left to name, or the meeting is over and the
 * backend runs the end-of-meeting pass itself. Its answers that mean "nothing
 * yet" pause them until the transcript grows instead, because a pass over the
 * same transcript cannot find anything the last one did not.
 */
const STOP_REASONS = ['ALREADY_NAMED', 'MEETING_ENDED'];
const PAUSE_REASONS = ['NO_EVIDENCE', 'NO_GROWTH', 'COOLDOWN'];

const PLATFORM_NAMED = 'Every voice already has a name from the meeting platform.';
const TIDY_HINT =
  "Use the one-person / several-people icon beside a name's voices to show them as one speaker or apart.";

/** How long a row opened from the meeting header stays outlined. */
const FOCUS_OUTLINE_MS = 3000;

/** A voice's number within its platform name ('Voice 2'), or null outside a name offered the choice. */
const voiceOrdinal = (row, group) => {
  if (!group || !row.voiceId) return null;
  const id = row.voiceId.replace(/[()]/g, '');
  return group.voices.find((voice) => voice.id === id)?.ordinal ?? null;
};

/** What the header says after a pass, by the resolver's reason. */
export const identifyOutcome = (result, roster) => {
  if (!result?.Success) return { type: 'error', text: 'Could not identify speakers' };
  if (result.NamesApplied > 0) {
    return {
      type: 'success',
      text: `Identified ${result.NamesApplied} ${result.NamesApplied === 1 ? 'speaker' : 'speakers'}`,
    };
  }
  if (result.Reason === 'ALREADY_NAMED') {
    // On a Virtual Participant meeting every voice carries the name the meeting
    // platform showed; what is left is how a name's voices are grouped.
    const fromPlatform = (roster ?? []).some((row) => !row.name);
    return {
      type: 'info',
      text: fromPlatform ? `${PLATFORM_NAMED} ${TIDY_HINT}` : 'Every voice already has a name.',
    };
  }
  if (result.Reason === 'COOLDOWN')
    return { type: 'info', text: 'Speakers were looked for moments ago. Try again shortly.' };
  if (result.Reason === 'MEETING_ENDED') return { type: 'info', text: 'The meeting has ended.' };
  // The honest answer, rather than silence: the transcript so far simply does
  // not say who these people are.
  return { type: 'info', text: 'Nothing in the transcript names them yet' };
};

const SpeakerRow = ({
  row,
  ordinal = null,
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
      .map((other) => ({ label: speakerRowLabel(other), value: other.key })),
  ];

  const nameField = candidates.length ? (
    <Autosuggest
      value={draft}
      onChange={({ detail }) => setDraft(detail.value)}
      onBlur={commit}
      onSelect={({ detail }) => onRename(row, (detail.value || '').trim())}
      options={candidates.map((name) => ({ value: name }))}
      enteredTextLabel={(value) => `Use "${value}"`}
      placeholder={row.group ? row.shownAs : row.defaultName}
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
      placeholder={row.group ? row.shownAs : row.defaultName}
      disabled={disabled || row.locked}
      ariaLabel={`Name for voice ${row.voiceId || row.channel}`}
    />
  );

  return (
    <SpaceBetween direction="vertical" size="xxs">
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Box variant="small">
          {ordinal !== null && (
            <span title={row.voiceId.replace(/[()]/g, '')}>
              <Badge>{`Voice ${ordinal}`}</Badge>
            </span>
          )}
          {ordinal === null &&
            (row.voiceId ? (
              <Badge>{row.voiceId.replace(/[()]/g, '')}</Badge>
            ) : (
              <Badge>{channelLabel(row.channel)}</Badge>
            ))}
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

const SpeakerIdentitiesPanel = ({
  item,
  segments,
  setToolsOpen,
  onSave,
  onIdentify,
  readOnly,
  isLive,
  onMeetingEnded = undefined,
  focusRequest = null,
  active = true,
}) => {
  const identities = useMemo(() => parseSpeakerIdentities(item.speakerIdentities), [item.speakerIdentities]);
  // How the meeting was captured, from its record: a Virtual Participant's names
  // are roster names, never a channel's own label (speaker-identities.js).
  const { captureSource } = item;
  const roster = useMemo(
    () => buildSpeakerRoster(segments, identities, item.remoteParticipantName, { captureSource }),
    [segments, identities, item.remoteParticipantName, captureSource],
  );
  // Each platform name's voices shown as one person or several, over the whole
  // transcript, and each row as the transcript shows it.
  const groups = useMemo(() => speakerGroups(segments, identities), [segments, identities]);
  const displayRows = useMemo(
    () => speakerDisplayRows(roster, identities, groups, item.remoteParticipantName),
    [roster, identities, groups, item.remoteParticipantName],
  );
  // The rows in the order first heard, the voices of a name offered the One
  // person / Several people choice gathered under it where its first voice is.
  const sections = useMemo(() => {
    const out = [];
    const byGroup = new Map();
    displayRows.forEach((row) => {
      const group = row.group ? own(groups, row.group) : undefined;
      if (!offersModeChoice(group)) {
        out.push({ row });
        return;
      }
      if (!byGroup.has(group.key)) {
        const section = { group, rows: [] };
        byGroup.set(group.key, section);
        out.push(section);
      }
      byGroup.get(group.key).rows.push(row);
    });
    return out;
  }, [displayRows, groups]);

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

  // The voices an automatic pass could still name, by the resolver's own rule
  // (nameableVoiceKeys, tested against the same table as the resolver). A voice
  // holding an unconfirmed SUGGESTION is still open, so a later
  // self-introduction can upgrade a guess; a roster name with a voice label is
  // not, because no pass will ever work on it.
  const unnamedCount = useMemo(
    () => nameableVoiceKeys(segments, identities, { captureSource }).length,
    [segments, identities, captureSource],
  );

  // Automatic passes stop for good when the resolver says nothing is left to
  // name (until a new voice appears) or that the meeting is over, and pause
  // after one that found nothing until the transcript grows. Read from the
  // interval callback through refs, like identifyingRef.
  const [stoppedAtVoices, setStoppedAtVoices] = useState(null);
  const autoStopped = stoppedAtVoices !== null && stoppedAtVoices === roster.length;
  const segmentCount = (segments ?? []).length;
  const segmentCountRef = useRef(segmentCount);
  segmentCountRef.current = segmentCount;
  const pausedAtSegmentsRef = useRef(null);
  const onMeetingEndedRef = useRef(onMeetingEnded);
  onMeetingEndedRef.current = onMeetingEnded;
  const rosterLengthRef = useRef(roster.length);
  rosterLengthRef.current = roster.length;

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
      const reason = result?.Reason;
      if (STOP_REASONS.includes(reason)) {
        setStoppedAtVoices(rosterLengthRef.current);
        // The page's copy of the meeting missed its end -- its subscription can
        // drop while a tab sits in the background -- so have it read again.
        if (reason === 'MEETING_ENDED') onMeetingEndedRef.current?.();
      } else if (PAUSE_REASONS.includes(reason) || (result?.Success && !result.NamesApplied)) {
        pausedAtSegmentsRef.current = segmentCountRef.current;
      } else {
        pausedAtSegmentsRef.current = null;
      }
      // A pass somebody asked for always says how it went. An automatic one
      // that was declined (the meeting is over, or a pass ran moments ago)
      // says nothing: nobody asked.
      const declined = STOP_REASONS.includes(reason) || reason === 'NO_GROWTH' || reason === 'COOLDOWN';
      if (force || !declined) setIdentifyStatus(identifyOutcome(result, roster));
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
   * here worth a model call" shortcut still applies. They also stop when the
   * resolver answers that nothing is left to name or that the meeting has
   * ended, and wait after a pass that found nothing until somebody speaks.
   */
  useEffect(() => {
    if (!isLive || readOnly || unnamedCount === 0 || autoStopped) {
      return undefined;
    }
    const timer = setInterval(() => {
      if (identifyingRef.current) return;
      if (pausedAtSegmentsRef.current !== null && pausedAtSegmentsRef.current === segmentCountRef.current) return;
      runIdentify(false);
    }, LIVE_PASS_INTERVAL_MS);
    return () => clearInterval(timer);
    // Keyed only on whether a pass is wanted at all. Neither runIdentify nor
    // onIdentify appears here on purpose: both change identity on every render,
    // so depending on either rebuilt the timer on every arriving transcript
    // segment and it only ever elapsed during silence -- precisely when there is
    // nothing new to read. Both are reached through refs instead.
  }, [isLive, readOnly, unnamedCount, autoStopped]);

  const namedCount = roster.filter((row) => row.name).length;

  // Starts open only on a channel separation actually split; once the user opens
  // or closes it themselves, their choice wins for the rest of the session.
  const [expandedOverride, setExpandedOverride] = useState(undefined);
  const expanded = expandedOverride ?? hasMultipleVoicesOnAChannel(roster, { captureSource, groups });

  // A speaker chosen in the meeting header: the panel opens, and that voice's
  // row is scrolled to, outlined for a moment and its name field focused.
  // Carried out once the Details tab is shown, since a hidden tab has no
  // layout to scroll; the request's time stamp tells one choice from the next.
  const rowsRef = useRef(null);
  const appliedFocusRef = useRef(null);
  const [focusedKey, setFocusedKey] = useState(null);
  const focusTimerRef = useRef(undefined);
  useEffect(() => () => clearTimeout(focusTimerRef.current), []);
  useEffect(() => {
    if (!focusRequest || appliedFocusRef.current === focusRequest.at) return;
    if (!expanded) {
      setExpandedOverride(true);
      return;
    }
    if (!active) return;
    appliedFocusRef.current = focusRequest.at;
    const rowElement = [...(rowsRef.current?.querySelectorAll('[data-speaker-key]') ?? [])].find(
      (element) => element.dataset.speakerKey === focusRequest.key,
    );
    if (!rowElement) return;
    rowElement.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
    rowElement.querySelector('input:not([disabled])')?.focus({ preventScroll: true });
    setFocusedKey(focusRequest.key);
    clearTimeout(focusTimerRef.current);
    focusTimerRef.current = setTimeout(() => setFocusedKey(null), FOCUS_OUTLINE_MS);
  }, [focusRequest, expanded, active]);

  const write = async (next) => {
    setSaving(true);
    try {
      await onSave(serializeSpeakerIdentities(next));
    } finally {
      setSaving(false);
    }
  };

  // Clearing the field removes the mapping, so the transcript falls back to
  // the voice id rather than showing an empty speaker. Under a name shown as
  // one person the transcript shows one name for all its voices, so naming one
  // names every voice under it that is not locked; otherwise two voices given
  // different names would leave the lines showing neither.
  const handleRename = (row, name) => {
    const group = row.group ? own(groups, row.group) : undefined;
    const keys =
      offersModeChoice(group) && group.mode === ONE_PERSON
        ? displayRows.filter((r) => r.key === row.key || (r.group === group.key && !r.locked)).map((r) => r.key)
        : [row.key];
    return write(keys.reduce((map, key) => withSpeakerName(map, key, name), identities));
  };

  const handleSetMode = (key, mode) => write(withSpeakerMode(identities, key, mode));

  const handleMerge = (row, targetKey) => write(withMerge(identities, row.key, targetKey));

  const handleUnmergeAll = (row) => write(withoutMerges(identities, row.mergedKeys));

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

  const renderRow = (row, ordinal) => (
    <div
      key={row.key}
      data-speaker-key={row.key}
      className={focusedKey === row.key ? 'speaker-row-focused' : undefined}
    >
      <SpaceBetween direction="vertical" size="xxs">
        <SpeakerRow
          row={row}
          ordinal={ordinal}
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
    </div>
  );

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
            // Through nativeAttributes: Cloudscape's Box accepts neither `role`
            // nor `ariaLive` as a prop, so passing them directly dropped both
            // and no live region ever reached the DOM.
            <Box nativeAttributes={{ role: 'status', 'aria-live': 'polite' }}>
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
          <div ref={rowsRef}>
            <SpaceBetween direction="vertical" size="s">
              {sections.map((section) =>
                section.group ? (
                  <div
                    key={`group:${section.group.key}`}
                    role="group"
                    aria-label={`Voices of ${section.group.base}`}
                    data-testid="speaker-group"
                  >
                    <SpaceBetween direction="vertical" size="xxs">
                      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                        <Box fontWeight="bold">{section.group.base}</Box>
                        <Box variant="small" color="text-body-secondary">
                          {`${section.group.voices.length} ${section.group.voices.length === 1 ? 'voice' : 'voices'}`}
                        </Box>
                        <SpeakerModeToggle
                          group={section.group}
                          onSetMode={readOnly || saving ? undefined : handleSetMode}
                        />
                      </SpaceBetween>
                      <div className="speaker-group-rows">
                        <SpaceBetween direction="vertical" size="xs">
                          {section.rows.map((row) => renderRow(row, voiceOrdinal(row, section.group)))}
                        </SpaceBetween>
                      </div>
                    </SpaceBetween>
                  </div>
                ) : (
                  renderRow(section.row, null)
                ),
              )}
            </SpaceBetween>
          </div>
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
