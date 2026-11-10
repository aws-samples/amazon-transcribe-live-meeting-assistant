/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { canonicalSentiment } from './channels';
import getRecordingStatus from './get-recording-status';
import { getSentimentTrendLabel, getWeightedSentimentLabel } from './sentiment';

/* Maps call attributes from API to a format that can be used in tables and panel */
// eslint-disable-next-line arrow-body-style
const mapCallsAttributes = (calls) => {
  return calls.map((item) => {
    const {
      CallId: callId,
      AgentId: agentId,
      CallSummaryText: callSummaryText,
      CallSummaryState: callSummaryState,
      SpeakerIdentities: speakerIdentities,
      Agenda: agenda,
      AgendaSource: agendaSource,
      AgendaUpdatedBy: agendaUpdatedBy,
      AgendaUpdatedAt: agendaUpdatedAt,
      TranscriptDeletionScheduledFor: transcriptDeletionScheduledFor,
      TranscriptDeletedAt: transcriptDeletedAt,
      TranscriptSegmentsDeleted: transcriptSegmentsDeleted,
      TranscriptFilesDeleted: transcriptFilesDeleted,
      // When the meeting record expires (epoch seconds), by the deployment's
      // days to keep meeting records.
      ExpiresAfter: expiresAfter,
      CreatedAt: callTimestamp,
      // The name given for the other participants (CustomerPhoneNumber is the
      // stored attribute's historical name); see resolveSpeaker.
      CustomerPhoneNumber: remoteParticipantName,
      // The label the capturing client gave the meeting's system side; a
      // Virtual Participant writes 'LMA System' (see meeting-context.js).
      SystemPhoneNumber: systemLabel,
      UpdatedAt: updatedAt,
      RecordingUrl: recordingUrl,
      VideoRecordingUrl: videoRecordingUrl,
      // How the meeting was captured and transcribed, as its START recorded it;
      // absent on a meeting started before these were stored (meeting-context.js).
      CaptureSource: captureSource,
      TranscriptionEngine: transcriptionEngine,
      TranscriptionLanguage: transcriptionLanguage,
      SpeakerSeparation: speakerSeparation,
      // The languages the speech engine identified, in the order first heard;
      // absent when it named none (detected-languages.js).
      DetectedLanguages: detectedLanguages,
      TotalConversationDurationMillis: totalConversationDurationMillis = 0,
      Sentiment: storedSentiment = {},
      Owner: owner,
      SharedWith: sharedWith,
      ListPK: listPK,
      ListSK: listSK,
    } = item;
    const recordingStatus = getRecordingStatus(item);
    // Keyed REMOTE and LOCAL, also for a meeting stored before 0.4.0 under CALLER
    // and AGENT. The caller*/agent* names below are the REMOTE and LOCAL values.
    const sentiment = canonicalSentiment(storedSentiment);

    const callerAverageSentiment = sentiment?.OverallSentiment?.REMOTE || 0;
    const callerSentimentLabel = getWeightedSentimentLabel(callerAverageSentiment);
    const agentAverageSentiment = sentiment?.OverallSentiment?.LOCAL || 0;
    const agentSentimentLabel = getWeightedSentimentLabel(agentAverageSentiment);

    const callerSentimentByQuarter = sentiment?.SentimentByPeriod?.QUARTER?.REMOTE || [];
    const callerSentimentTrendLabel = getSentimentTrendLabel(callerSentimentByQuarter);
    const agentSentimentByQuarter = sentiment?.SentimentByPeriod?.QUARTER?.LOCAL || [];
    const agentSentimentTrendLabel = getSentimentTrendLabel(agentSentimentByQuarter);

    return {
      callId,
      agentId,
      callSummaryText,
      // Raw JSON string; parsed by the readers via parseSummaryState, so a
      // meeting with no state at all reads as a generated summary with nothing
      // running rather than breaking the mapping.
      callSummaryState,
      // Raw JSON string; parsed by the readers via parseSpeakerIdentities so a
      // malformed blob degrades to "no names" instead of breaking the mapping.
      speakerIdentities,
      agenda,
      agendaSource,
      agendaUpdatedBy,
      agendaUpdatedAt,
      transcriptDeletionScheduledFor,
      transcriptDeletedAt,
      transcriptSegmentsDeleted,
      transcriptFilesDeleted,
      expiresAfter,
      remoteParticipantName,
      systemLabel,
      updatedAt,
      recordingUrl,
      videoRecordingUrl,
      captureSource,
      transcriptionEngine,
      transcriptionLanguage,
      speakerSeparation,
      detectedLanguages: Array.isArray(detectedLanguages) ? detectedLanguages.filter(Boolean) : [],
      totalConversationDurationMillis,
      conversationDurationTimeStamp: new Date(totalConversationDurationMillis).toISOString().substr(11, 8),
      sentiment,
      // change callTimestamp to a sortable date format
      initiationTimeStamp: new Date(callTimestamp).toISOString(),
      recordingStatusLabel: recordingStatus.label,
      recordingStatusIcon: recordingStatus.icon,
      callerAverageSentiment,
      callerSentimentLabel,
      callerSentimentTrendLabel,
      agentAverageSentiment,
      agentSentimentLabel,
      agentSentimentTrendLabel,
      owner,
      sharedWith,
      listPK,
      listSK,
    };
  });
};

/**
 * A newer copy of the meeting a page shows (mapped), keeping from the copy on
 * screen the fields that only getCall selects when the newer copy has none.
 *
 * The meeting page reads its meeting with getCall, then takes up the copy in
 * the shared meeting list whenever the meeting changes. That copy is built from
 * the list query and the onUpdateCall subscription, and neither carries these
 * fields for an update the Kinesis pipeline made: a page opened during a live
 * meeting would otherwise lose them at the first transcript update.
 *
 * The agenda is one unit keyed on agendaUpdatedAt, as in mergeCallUpdate:
 * every agenda write sets that timestamp, so a copy that has one is
 * authoritative for all four fields, including an agenda that was cleared.
 */
export const adoptMeetingCopy = (current, incoming) => {
  if (!current || !incoming) return incoming;
  const keep = (field) =>
    incoming[field] === undefined || incoming[field] === null ? current[field] : incoming[field];
  return {
    ...incoming,
    ...(incoming.agendaUpdatedAt
      ? {}
      : {
          agenda: current.agenda,
          agendaSource: current.agendaSource,
          agendaUpdatedBy: current.agendaUpdatedBy,
          agendaUpdatedAt: current.agendaUpdatedAt,
        }),
    captureSource: keep('captureSource'),
    transcriptionEngine: keep('transcriptionEngine'),
    transcriptionLanguage: keep('transcriptionLanguage'),
    speakerSeparation: keep('speakerSeparation'),
    transcriptDeletionScheduledFor: keep('transcriptDeletionScheduledFor'),
    transcriptDeletedAt: keep('transcriptDeletedAt'),
    transcriptSegmentsDeleted: keep('transcriptSegmentsDeleted'),
    transcriptFilesDeleted: keep('transcriptFilesDeleted'),
    expiresAfter: keep('expiresAfter'),
  };
};

/** Whether a newer copy of a meeting carries a different agenda from the copy on screen. */
export const agendaChanged = (current, incoming) =>
  Boolean(incoming?.agendaUpdatedAt) && incoming.agendaUpdatedAt !== current?.agendaUpdatedAt;

export default mapCallsAttributes;
