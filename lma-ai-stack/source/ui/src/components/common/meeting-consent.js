/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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

/** The Cloudscape Badge color for a verifier verdict. */
export const verdictColor = (verdict) => ({ supports_consent: 'green', objection_found: 'red' }[verdict] || 'grey');

/** A display name as the backend matches replies to participants by. */
export const normalizeName = (name) =>
  typeof name === 'string' ? name.normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim() : '';

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
 * One row per participant: everyone listed as present now, then anybody else
 * who replied or was listed earlier, each with their latest reply.
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
    rows.push({ name, present: present.has(key), decision: decisions.get(key) || null });
  };
  (consent.Participants || []).forEach(add);
  (consent.ParticipantsSeen || []).forEach(add);
  (consent.Events || []).filter((e) => e && e.Source !== 'attestation').forEach((e) => add(e.ParticipantName));
  return rows;
};

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
  return entries
    .map((entry, index) => ({ ...entry, index }))
    .sort((a, b) => {
      if (a.at && b.at && a.at !== b.at) return a.at < b.at ? -1 : 1;
      if (!a.at !== !b.at) return a.at ? -1 : 1;
      return (order[a.kind] ?? 9) - (order[b.kind] ?? 9) || a.index - b.index;
    });
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
  if (rows.length) {
    lines.push('', 'Participants:');
    rows.forEach((row) => {
      const reply = row.decision ? row.decision : 'no reply';
      lines.push(`- ${row.name}: ${reply}${row.present ? '' : ' (not present at the end)'}`);
    });
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
      const check = item.Verification
        ? `${VERDICT_LABELS[item.Verification.Verdict] || item.Verification.Verdict} (model ${
            item.Verification.ModelId
          }, confidence ${item.Verification.Confidence})`
        : 'not checked';
      lines.push(`- ${item.Kind || 'screenshot'} captured ${item.CapturedAt || 'at an unknown time'}: ${check}`);
    });
  }
  lines.push(
    '',
    'This record is supporting evidence of what was posted and replied in the meeting, by display name. ' +
      'It is not a legal determination that consent was given.',
  );
  return lines.join('\n');
};
