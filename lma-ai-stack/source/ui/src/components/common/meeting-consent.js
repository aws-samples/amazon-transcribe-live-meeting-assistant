/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Reading a meeting's consent record (Call.Consent) for the meeting page and
 * the summary download. The record is written and its status decided by the
 * backend (lma_consent in the configuration layer); nothing here decides
 * anything, it only labels and orders what the record says. See
 * docs/meeting-consent.md.
 */

export const MODE_LABELS = {
  off: 'Off',
  notify: 'Notify (participants may object)',
  opt_in: 'Opt-in (consent needed first)',
};

export const RULE_LABELS = {
  each_participant: 'Each participant consents',
  owner_attests: 'The meeting owner attests that everyone agreed',
};

export const STATUS_LABELS = {
  pending: 'Pending',
  granted: 'Granted',
  objected: 'Objected',
  not_required: 'Not required',
};

/** The Cloudscape StatusIndicator type for a status. */
export const statusIndicatorType = (status) =>
  ({ granted: 'success', objected: 'error', pending: 'pending', not_required: 'stopped' }[status] || 'info');

export const SOURCE_LABELS = {
  chat: 'meeting chat',
  voice: 'spoken',
  ui: 'entered on the meeting page',
  attestation: 'owner attestation',
};

export const ATTESTATION_METHOD_LABELS = {
  verbal: 'Everyone agreed out loud',
  chat: 'Everyone agreed in the meeting chat',
  invite_terms: "Everyone accepted the invitation's terms",
};

export const VERDICT_LABELS = {
  supports_consent: 'Supports consent',
  objection_found: 'Objection found',
  inconclusive: 'Inconclusive',
};

/**
 * Whether an evidence image is a participant list on its own. The Virtual
 * Participant never asks for these to be checked automatically: the check reads
 * a chat for the notice and the replies. Told by the view in the key the VP
 * stores it under (`…-participants-….jpg`) or by its note.
 */
export const isParticipantListEvidence = (item) =>
  !!item && (/-participants-[^/]*$/.test(item.S3Key || '') || /^Participant list\b/.test(item.Note || ''));

/** What an image with no check says instead of a verdict. */
export const uncheckedEvidenceLabel = (item) =>
  isParticipantListEvidence(item) ? 'Not checked: participant lists are kept as a record' : 'Image check: not run';

/** "Consented aloud or in LMA, so not expected in this image: Bo Chen", or '' when nobody. */
export const consentedOutsideChatLabel = (verification) => {
  const names = (verification && verification.ConsentedOutsideChat) || [];
  return names.length ? `Consented aloud or in LMA, so not expected in this image: ${names.join(', ')}` : '';
};

/** The Cloudscape Badge color for a verifier verdict. */
export const verdictColor = (verdict) => ({ supports_consent: 'green', objection_found: 'red' }[verdict] || 'grey');

/**
 * A display name as the backend matches replies to participants by: NFKC, then
 * case folded, with spacing collapsed. JavaScript has no casefold, so the two
 * most common characters lower-casing leaves different from Python's
 * casefold() -- the sharp s and the final sigma -- are folded here by hand;
 * rarer ones are not.
 */
export const normalizeName = (name) =>
  typeof name === 'string'
    ? name.normalize('NFKC').toLowerCase().replace(/ß/g, 'ss').replace(/ς/g, 'σ').replace(/\s+/g, ' ').trim()
    : '';

/**
 * Each participant's latest reply, keyed by normalised name, in the order the
 * replies were recorded; attestation entries are not a participant's reply.
 * The same rule the backend decides the status by.
 */
export const latestDecisions = (events) => {
  const latest = new Map();
  (events || []).forEach((event) => {
    if (!event || event.Source === 'attestation') return;
    const key = normalizeName(event.ParticipantName);
    if (key && (event.Decision === 'consent' || event.Decision === 'objection')) {
      latest.set(key, event.Decision);
    }
  });
  return latest;
};

/**
 * Whether a name is one of the seats the Virtual Participant lists for people
 * it has counted but not yet seen by name, on platforms that give only an
 * attendee count ("Unidentified participant 2"; consent-roster.ts in the
 * Virtual Participant). Such a seat is not a person: the same seat number can
 * stand for somebody else after the count changes. "Unidentified participant"
 * with no number is how a reply with no sender is recorded, and is not a seat.
 */
export const isPlaceholderSeat = (name) => /^unidentified participant \d+$/.test(normalizeName(name));

/**
 * One row per participant: everyone listed as present now, then anybody else
 * who replied or was listed earlier, each with their latest reply.
 *
 * Placeholder seats (isPlaceholderSeat) are not rows: one that is present is
 * counted by unidentifiedPresentCount instead, and one that is no longer
 * present is not shown at all. A seat with a reply recorded under its name
 * stays a row, so the record's replies are all shown.
 */
export const participantRows = (consent) => {
  if (!consent) return [];
  const decisions = latestDecisions(consent.Events);
  const present = new Set((consent.Participants || []).map(normalizeName));
  const rows = [];
  const seen = new Set();
  const add = (name) => {
    const key = normalizeName(name);
    if (!key || seen.has(key)) return;
    seen.add(key);
    const decision = decisions.get(key) || null;
    if (isPlaceholderSeat(name) && !decision) return;
    rows.push({ name, present: present.has(key), decision });
  };
  (consent.Participants || []).forEach(add);
  (consent.ParticipantsSeen || []).forEach(add);
  (consent.Events || []).filter((e) => e && e.Source !== 'attestation').forEach((e) => add(e.ParticipantName));
  return rows;
};

/** How many placeholder seats are listed as present now, with no reply under their name. */
export const unidentifiedPresentCount = (consent) => {
  if (!consent) return 0;
  const decisions = latestDecisions(consent.Events);
  const seats = new Set(
    (consent.Participants || [])
      .filter((name) => isPlaceholderSeat(name) && !decisions.get(normalizeName(name)))
      .map(normalizeName),
  );
  return seats.size;
};

/** "1 participant not yet identified", "2 participants not yet identified". */
export const unidentifiedLabel = (count) => `${count} participant${count === 1 ? '' : 's'} not yet identified`;

const describeEvent = (event) => {
  const who = event.ParticipantName || 'Someone';
  const where = SOURCE_LABELS[event.Source] || event.Source || '';
  if (event.Source === 'attestation') {
    return `${who} attested that everyone agreed${event.Text ? `: "${event.Text}"` : ''}`;
  }
  const verb = event.Decision === 'objection' ? 'objected' : 'consented';
  const words = event.Text ? `: "${event.Text}"` : '';
  const by = event.Source === 'ui' && event.RecordedBy ? `, recorded by ${event.RecordedBy}` : '';
  return `${who} ${verb} (${where}${by})${words}`;
};

/**
 * Everything that happened, oldest first: notices posted, replies and
 * attestations, and each change of status. Entries without a time keep their
 * place after the timed ones.
 */
export const buildTimeline = (consent) => {
  if (!consent) return [];
  const entries = [];
  if (consent.StartedAt) {
    entries.push({
      at: consent.StartedAt,
      kind: 'started',
      text: `Consent started in mode ${MODE_LABELS[consent.Mode] || consent.Mode}${
        consent.StartedBy ? ` by ${consent.StartedBy}` : ''
      }`,
    });
  }
  (consent.Notices || []).forEach((notice) => {
    entries.push({
      at: notice.PostedAt,
      kind: 'notice',
      text: `Notice posted (${notice.Channel || 'unknown channel'})${notice.PostedBy ? ` by ${notice.PostedBy}` : ''}`,
    });
  });
  (consent.Events || []).forEach((event) => {
    entries.push({
      at: event.OccurredAt || event.RecordedAt,
      // Ordered by when the backend recorded it, as the status is decided;
      // a client's own clock, which OccurredAt is, can be off.
      orderAt: event.RecordedAt || event.OccurredAt,
      kind: event.Source === 'attestation' ? 'attestation' : event.Decision,
      text: describeEvent(event),
    });
  });
  (consent.StatusHistory || []).forEach((change) => {
    entries.push({
      at: change.At,
      kind: 'status',
      text: `Status ${STATUS_LABELS[change.Status] || change.Status}: ${
        change.CaptureAllowed ? 'recording and transcription allowed' : 'recording and transcription withheld'
      }${change.Reason ? ` (${change.Reason})` : ''}`,
    });
  });
  const order = { started: 0, notice: 1, attestation: 2, consent: 2, objection: 2, status: 3 };
  // Compared as instants, not as strings: "10:00:00.500Z" sorts before
  // "10:00:00Z" as text, and an offset other than Z sorts by its local digits.
  const instant = (entry) => {
    const ms = Date.parse(entry.orderAt || entry.at || '');
    return Number.isNaN(ms) ? null : ms;
  };
  return entries
    .map((entry, index) => ({ ...entry, index, ms: instant(entry) }))
    .sort((a, b) => {
      if (a.ms !== null && b.ms !== null && a.ms !== b.ms) return a.ms - b.ms;
      if ((a.ms === null) !== (b.ms === null)) return a.ms === null ? 1 : -1;
      return (order[a.kind] ?? 9) - (order[b.kind] ?? 9) || a.index - b.index;
    })
    .map(({ ms, orderAt, ...entry }) => entry);
};

/** The record as plain text, for the summary download and the Word document. */
export const consentAsText = (consent) => {
  if (!consent) return '';
  const lines = ['CONSENT', ''];
  lines.push(`Mode: ${MODE_LABELS[consent.Mode] || consent.Mode || 'unknown'}`);
  if (consent.Rule) lines.push(`Rule: ${RULE_LABELS[consent.Rule] || consent.Rule}`);
  lines.push(`Status: ${STATUS_LABELS[consent.Status] || consent.Status || 'unknown'}`);
  lines.push(
    `Recording and transcription: ${consent.CaptureAllowed ? 'allowed' : 'withheld'} at the time of this download`,
  );
  if (consent.NoticeText) lines.push(`Notice: ${consent.NoticeText}`);
  if (consent.Attestation) {
    const a = consent.Attestation;
    lines.push(
      `Attestation: ${a.AttestedBy || 'the owner'}, ${ATTESTATION_METHOD_LABELS[a.Method] || a.Method}${
        a.AttestedAt ? `, ${a.AttestedAt}` : ''
      }${a.Statement ? ` ("${a.Statement}")` : ''}`,
    );
  }
  const rows = participantRows(consent);
  const unidentified = unidentifiedPresentCount(consent);
  if (rows.length || unidentified) {
    lines.push('', 'Participants:');
    rows.forEach((row) => {
      const reply = row.decision ? row.decision : 'no reply';
      lines.push(`- ${row.name}: ${reply}${row.present ? '' : ' (not present at the time of this download)'}`);
    });
    if (unidentified) lines.push(`- ${unidentifiedLabel(unidentified)}`);
  }
  const timeline = buildTimeline(consent);
  if (timeline.length) {
    lines.push('', 'Timeline:');
    timeline.forEach((entry) => lines.push(`- ${entry.at || '(no time)'} ${entry.text}`));
  }
  const evidence = consent.Evidence || [];
  if (evidence.length) {
    lines.push('', 'Evidence:');
    evidence.forEach((item) => {
      const outside = consentedOutsideChatLabel(item.Verification);
      const check = item.Verification
        ? `Image check: ${VERDICT_LABELS[item.Verification.Verdict] || item.Verification.Verdict} (model ${
            item.Verification.ModelId
          }, confidence ${item.Verification.Confidence})${outside ? `. ${outside}` : ''}`
        : uncheckedEvidenceLabel(item);
      // What the image shows, from its note, then when it was captured; the
      // check reports what the image shows, which can differ from the status.
      const captured = `${item.Kind || 'screenshot'} captured ${item.CapturedAt || 'at an unknown time'}`;
      const note = (item.Note || '').replace(/\s+/g, ' ').trim();
      lines.push(`- ${note ? `${note} (${captured})` : captured}. ${check}`);
    });
  }
  lines.push(
    '',
    'This record is supporting evidence of what was posted and replied in the meeting, by display name. ' +
      'It is not a legal determination that consent was given.',
  );
  return lines.join('\n');
};
