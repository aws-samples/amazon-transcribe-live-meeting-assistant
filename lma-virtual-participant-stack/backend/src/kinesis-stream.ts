import { KinesisClient, PutRecordCommand, PutRecordsCommand } from '@aws-sdk/client-kinesis';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { details } from './details.js';
import { VoiceLabelNames, buildDiarizedSegments, type TranscribeItem } from './transcribe-diarization.js';
import { agendaStartFields, type VpAgenda } from './meeting-agenda.js';

export interface TranscriptSegment {
  channel: string;
  segmentId: string;
  startTime: number;
  endTime: number;
  transcript: string;
  isPartial: boolean;
  speaker?: string;
} 

/** The fields of an ADD_VIDEO_KEYFRAME event other than its envelope. */
export interface VideoKeyframeEvent {
  /** Sortable id: the timeline position in milliseconds, zero-padded, plus a suffix. */
  FrameId: string;
  /** Seconds on the transcript timeline (the clock of the segments' StartTime). */
  Timestamp: number;
  /** Seconds into the meeting's video recording, or null when there is none. */
  VideoOffset: number | null;
  /** HTTPS S3 URLs, in the same shape as the recording URLs. */
  ImageUrl: string;
  ThumbnailUrl: string;
  Width: number;
  Height: number;
  Source: 'VIRTUAL_PARTICIPANT';
  /** Whether the platform showed a screen share; null where it gives no signal. */
  ShareDetected: boolean | null;
  /** Fraction of the picture that differs from the previous key frame. */
  ChangeScore: number;
}

export interface CallMetaData {
  callId: string;
  customerPhoneNumber?: string;
  systemPhoneNumber?: string;
  agentId?: string;
  callStreamingStartTime?: string;
  callStreamingEndTime?: string;
  callCategories?: string[];
  callCategoryScore?: number;
  issuesDetected?: string[];
  actionItemsDetected?: string[];
  callSummaryText?: string;
  recordingUrl?: string;
  pcaUrl?: string;
  sentiment?: {
    OverallSentiment: {
      AGENT?: number;
      CUSTOMER?: number;
    };
    SentimentByPeriod: {
      QUARTER?: {
        AGENT?: Array<{ Score: number; BeginOffsetMillis: number; EndOffsetMillis: number }>;
        CUSTOMER?: Array<{ Score: number; BeginOffsetMillis: number; EndOffsetMillis: number }>;
      };
    };
  };
}

export interface KinesisRecord {
  eventType: 'START' | 'CONTINUE' | 'END' | 'ADD_TRANSCRIPT_SEGMENT' | 'ADD_CALL_CATEGORY' | 'ADD_REAL_TIME_CALL_ANALYTICS_METADATA';
  callId: string;
  channel?: string;
  segmentId?: string;
  startTime?: number;
  endTime?: number;
  transcript?: string;
  isPartial?: boolean;
  speaker?: string;
  callMetaData?: CallMetaData;
  recordingUrl?: string;
  timestamp: number;
}

// Local testing mode
const isLocalTest = process.env.LOCAL_TEST === 'true';

/**
 * Whether to emit per-result diagnostics.
 *
 * `DEBUG` is honoured because that is the habit for a local run; `LOG_LEVEL`
 * is what a deployment actually sets, through the stack's LogLevel parameter.
 * Gating only on the former meant the separation log could not be switched on
 * in a deployment at all -- which is the only place a real room's audio exists
 * to be diagnosed.
 *
 * Used only by the diarized path, which logs participant names and no
 * transcript text. The two `process.env.DEBUG` gates elsewhere in this file
 * are deliberately separate and keep their own condition.
 */
const diagnosticsOn = (): boolean =>
  Boolean(process.env.DEBUG) || (process.env.LOG_LEVEL || '').toUpperCase() === 'DEBUG';

class KinesisStreamManager {
  private kinesisClient: KinesisClient;
  private callId: string;

  /**
   * The agenda the VP was started or scheduled with, read from its row before
   * the meeting starts; see meeting-agenda.ts. Null when there is none.
   */
  private agenda: VpAgenda | null = null;

  setAgenda(agenda: VpAgenda | null): void {
    this.agenda = agenda;
  }

  /** The meeting's CallId, so other services (the ASR launcher lease) can key on it. */
  get currentCallId(): string {
    return this.callId;
  }
  private streamName: string;
  
  // Who the platform showed over a span of the meeting timeline, supplied by
  // the transcription service from its roster timeline (roster-timeline.ts).
  // Undefined, or no lookup at all, means nobody is known yet, and the name
  // the caller passed is used instead.
  private rosterLookup: ((absoluteStart: number, absoluteEnd: number) => string | undefined) | null = null;
  // Whether to recover speaker turns from Transcribe's per-item labels. Off
  // unless the deployment's VP voice-separation switch is on.
  private diarize = false;
  // Which participant each Transcribe voice label is shown under. Labels
  // restart with every Transcribe session, and each session has its own
  // timeline offset, so a new offset starts a new map.
  private voiceLabels = new VoiceLabelNames();
  private voiceLabelsOffset: number | null = null;

  constructor() {
    // In local test mode, explicitly use default provider which checks credentials file first
    // Otherwise use default credential chain (EC2 instance role in production)
    const clientConfig: any = {
      region: process.env.AWS_REGION || 'us-east-1',
    };
    
    if (isLocalTest) {
      console.log('Using AWS credentials from ~/.aws/credentials for Kinesis');
      // Default provider checks credentials file, then environment variables, then instance metadata
      clientConfig.credentials = defaultProvider();
    }
    
    this.kinesisClient = new KinesisClient(clientConfig);
    
    // Use  CallId format: meeting_name_with_timestamp
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').replace('T', '-').replace('Z', '');
    this.callId = `${details.invite.meetingName}-${timestamp}`;
    this.streamName = details.callDataStreamName;
    
    if (!this.streamName) {
      console.warn('CALL_DATA_STREAM_NAME not configured - Kinesis streaming disabled');
    }
  }

  /**
   * Writes one record. Errors are logged and swallowed, so a lost event never
   * ends the meeting -- except with `rethrow`, for a caller that has something
   * to undo when the record was not written (see sendVideoKeyframe).
   */
  private async sendRecord(record: any, { rethrow = false }: { rethrow?: boolean } = {}): Promise<void> {
    if (!this.streamName) {
      console.log('Kinesis streaming disabled - would send:', record.EventType);
      return;
    }

    try {
      const command = new PutRecordCommand({
        StreamName: this.streamName,
        Data: Buffer.from(JSON.stringify(record)),
        PartitionKey: this.callId,
      });

      const result = await this.kinesisClient.send(command);
      console.log(`Kinesis record sent: ${record.EventType}, SequenceNumber: ${result.SequenceNumber}`);
    } catch (error: any) {
      // Handle permission errors gracefully (like Python version)
      if (error.name === 'AccessDeniedException') {
        console.log(`Error sending ${record.EventType} event to Kinesis: ${error.message}`);
        console.log('Note: This is expected during local testing without proper IAM permissions');
      } else {
      // nosemgrep: javascript.lang.security.audit.unsafe-formatstring.unsafe-formatstring -- ECMAScript template literals do not interpret util.format specifiers
        console.error(`Error sending ${record.EventType} event to Kinesis:`, error);
      }
      if (rethrow) throw error;
      // Don't throw error - continue execution like Python version
    }
  }

  async sendStartMeeting(): Promise<void> {
    // Match Python kds.py format exactly
    const record: any = {
      EventType: 'START',
      CallId: this.callId,
      CustomerPhoneNumber: 'Virtual Participant',
      SystemPhoneNumber: 'LMA System',
      AgentId: details.lmaUser, // This will be used as owner by CallEventProcessor
      CreatedAt: new Date().toISOString(),
      AccessToken: process.env.USER_ACCESS_TOKEN || '',
      IdToken: process.env.USER_ID_TOKEN || '',
      RefreshToken: process.env.USER_REFRESH_TOKEN || '',
      // Created with the meeting, so it is there from the first event; nothing
      // is added when the VP has no agenda.
      ...agendaStartFields(this.agenda, details.lmaUser),
    };

    await this.sendRecord(record);
    
    // Link CallId to VP record (matching Python kds.py behavior)
    const vpId = details.invite.virtualParticipantId;
    if (vpId) {
        try {
            await this.linkCallToVP(vpId, this.callId);
            console.log(`Linked CallId ${this.callId} to VP ${vpId}`);
        } catch (error) {
            console.log(`Failed to link CallId to VP: ${error}`);
        }
    }
  }

  /**
   * Link the CallId onto the VP record.
   *
   * Delegates to the status manager so there is one implementation of this
   * write. It used to have its own: read the row's status, then send it back
   * with the CallId. That is a read-modify-write of a field this call does not
   * own, and unlike the equivalent at startup it runs here in the middle of the
   * VP's life — `startTranscription` calls it moments before `markActive`
   * reports ACTIVE, on the same code path — so losing that race put a
   * recording VP back to whatever the read had returned.
   *
   * The CallId is already linked before the meeting starts (see `index.ts`,
   * which calls `setCallId` with this same value). This remains as a
   * belt-and-braces write for a VP whose startup link did not happen.
   */
  private async linkCallToVP(vpId: string, callId: string): Promise<void> {
    if (!details.graphqlEndpoint) {
        console.log('Warning: GRAPHQL_ENDPOINT not set, cannot link CallId');
        return;
    }
    const { createStatusManager } = await import('./status-manager.js');
    const linked = await createStatusManager(vpId).setCallId(callId);
    if (!linked) {
        throw new Error(`Failed to link CallId ${callId} to VP ${vpId}`);
    }
  }

  async sendEndMeeting(recordingUrl?: string): Promise<void> {
    // Match Python kds.py format exactly
    const record: any = {
      EventType: 'END',
      CallId: this.callId,
      CustomerPhoneNumber: 'Customer Phone',
      SystemPhoneNumber: 'System Phone',
      CreatedAt: new Date().toISOString(),
      AccessToken: process.env.USER_ACCESS_TOKEN || '',
      IdToken: process.env.USER_ID_TOKEN || '',
      RefreshToken: process.env.USER_REFRESH_TOKEN || '',
    };

    await this.sendRecord(record);
  }

  async sendCallRecording(url: string): Promise<void> {
    // Match Python kds.py send_call_recording function
    const record: any = {
      EventType: 'ADD_S3_RECORDING_URL',
      CallId: this.callId,
      RecordingUrl: url,
      AccessToken: process.env.USER_ACCESS_TOKEN || '',
      IdToken: process.env.USER_ID_TOKEN || '',
      RefreshToken: process.env.USER_REFRESH_TOKEN || '',
    };

    await this.sendRecord(record);
    console.log(`Sent call recording event to Kinesis.`);
  }

  async sendCallVideoRecording(url: string): Promise<void> {
    // Parallel to sendCallRecording: ADD_S3_VIDEO_RECORDING_URL carries the
    // screen-recording .mp4 URL so the call_event_processor persists it on the
    // Call record's VideoRecordingUrl field (separate from the audio
    // RecordingUrl so the two never clobber each other).
    const record: any = {
      EventType: 'ADD_S3_VIDEO_RECORDING_URL',
      CallId: this.callId,
      VideoRecordingUrl: url,
      AccessToken: process.env.USER_ACCESS_TOKEN || '',
      IdToken: process.env.USER_ID_TOKEN || '',
      RefreshToken: process.env.USER_REFRESH_TOKEN || '',
    };

    await this.sendRecord(record);
    console.log(`Sent call video recording event to Kinesis.`);
  }

  /**
   * ADD_VIDEO_KEYFRAME: one key frame of the meeting display (see
   * keyframe-sampler.ts). The call_event_processor stores it on the meeting and
   * publishes it on the onAddVideoKeyframe subscription. Throws when the record
   * was not written, so the sampler can treat the frame as not saved.
   */
  async sendVideoKeyframe(frame: VideoKeyframeEvent): Promise<void> {
    const record: any = {
      EventType: 'ADD_VIDEO_KEYFRAME',
      CallId: this.callId,
      ...frame,
      CreatedAt: new Date().toISOString(),
      AccessToken: process.env.USER_ACCESS_TOKEN || '',
      IdToken: process.env.USER_ID_TOKEN || '',
      RefreshToken: process.env.USER_REFRESH_TOKEN || '',
    };
    await this.sendRecord(record, { rethrow: true });
  }

  // Returns the highest absolute segment EndTime (seconds) written by this call
  // so the caller can advance the cumulative reconnect offset. Returns
  // timeOffsetSeconds unchanged when nothing was written.
  async sendTranscriptSegment(speaker: string, transcriptResult: any, timeOffsetSeconds = 0): Promise<number> {
    console.log("Process speaker changes to identify segments within result");
    const segments = this.processTranscriptionResults(speaker, transcriptResult, timeOffsetSeconds);
    let maxEndTime = timeOffsetSeconds;

    for (const segment of Object.values(segments)) {
      const segEnd = (segment as any).EndTime;
      if (typeof segEnd === 'number' && segEnd > maxEndTime) {
        maxEndTime = segEnd;
      }
      if (process.env.DEBUG) {
        console.log(`Sending ADD_TRANSCRIPT_SEGMENT event to Kinesis. Segment:`, segment);
      }

      try {
        const record: any = {
          EventType: 'ADD_TRANSCRIPT_SEGMENT',
          CallId: this.callId,
          Channel: 'CALLER',
          SegmentId: (segment as any).SegmentId,
          StartTime: (segment as any).StartTime !== null ? (segment as any).StartTime : 0,
          EndTime: (segment as any).EndTime !== null ? (segment as any).EndTime : 0,
          Transcript: (segment as any).Transcript,
          IsPartial: KinesisStreamManager.isPartialResult(transcriptResult),
          CreatedAt: new Date().toISOString(),
          UpdatedAt: new Date().toISOString(),
          Sentiment: null,
          TranscriptEvent: null,
          UtteranceEvent: null,
          Speaker: (segment as any).Speaker,
          AccessToken: process.env.USER_ACCESS_TOKEN || '',
          IdToken: process.env.USER_ID_TOKEN || '',
          RefreshToken: process.env.USER_REFRESH_TOKEN || '',
        };

        await this.sendRecord(record);
        console.log(`Sent ADD_TRANSCRIPT_SEGMENT event to Kinesis.`);
      } catch (error) {
        console.log(`Error sending ADD_TRANSCRIPT_SEGMENT event to Kinesis: ${error}`);
      }
    }
    return maxEndTime;
  }

  /**
   * One row from the MicroVM ASR engine. Unlike an Amazon Transcribe result, the
   * engine's rows arrive already segmented - one per utterance or speaker turn, with
   * an id a later final reuses to update the row in place - so nothing here has to
   * split on speaker changes; the caller supplies the finished speaker name.
   */
  async sendAsrTranscriptSegment(segment: TranscriptSegment): Promise<void> {
    const now = new Date().toISOString();
    const record: any = {
      EventType: 'ADD_TRANSCRIPT_SEGMENT',
      CallId: this.callId,
      Channel: segment.channel || 'CALLER',
      SegmentId: segment.segmentId,
      StartTime: segment.startTime,
      EndTime: segment.endTime,
      Transcript: segment.transcript,
      IsPartial: segment.isPartial,
      CreatedAt: now,
      UpdatedAt: now,
      Sentiment: null,
      TranscriptEvent: null,
      UtteranceEvent: null,
      Speaker: segment.speaker,
      AccessToken: process.env.USER_ACCESS_TOKEN || '',
      IdToken: process.env.USER_ID_TOKEN || '',
      RefreshToken: process.env.USER_REFRESH_TOKEN || '',
    };
    await this.sendRecord(record);
  }

  /**
   * Turn on recovery of speaker turns from Transcribe's per-item labels.
   *
   * Set once per meeting from the deployment's VP voice-separation switch, so
   * the two engines honour the same control: the on-demand engine reads it when
   * opening its session, and this path reads it here.
   */
  setDiarization(enabled: boolean): void {
    this.diarize = enabled === true;
    // Set once per meeting, so a new meeting starts with no label owners.
    this.voiceLabels = new VoiceLabelNames();
    this.voiceLabelsOffset = null;
  }

  /**
   * Name rows after who the platform showed during their audio.
   *
   * The lookup is keyed on the meeting timeline -- Transcribe's item times plus
   * the session offset -- and the transcription service records each
   * participant change on that same clock at the moment the platform reports
   * it. Rows used to be named from changes stamped at the last word of
   * whichever result was in hand when the change was noticed, which lands a
   * partial's latency (most of a second) before the change and gave the
   * previous speaker's last words to the next one.
   */
  setRosterLookup(lookup: ((absoluteStart: number, absoluteEnd: number) => string | undefined) | null): void {
    this.rosterLookup = lookup;
  }

  /**
   * Whether a result is still in progress.
   *
   * Derived once and shared, because the places that used to decide it
   * independently did not agree: the builder tested `=== true`, the record
   * written to Kinesis used `|| false`, and the resets used `!`. Any truthy
   * non-boolean therefore split a result as if it were final while every
   * resulting row was stamped in progress. The SDK deserialises this field to
   * a real boolean, so it was unreachable through Amazon Transcribe -- but a
   * property that holds "by construction" should not rest on a wire type.
   */
  private static isPartialResult(result: any): boolean {
    return Boolean(result?.IsPartial);
  }

  /**
   * A stable id for a result that arrived without one.
   *
   * Keyed on the result's own start time, falling back to the first timed
   * item's, because that is the one thing a partial and its final both carry:
   * a final repeats its partial's items, so the first item's time is the same,
   * while two different results start at different moments. Two requirements
   * have to hold at once -- a partial and its final must share an id, and two
   * different results must not -- and anything stateful fails one of them. A
   * counter makes every delivery unique, which orphans the partial; memoising
   * on the result object does not help either, because the SDK deserialises a
   * fresh object per TranscriptEvent, so a partial and its final are never the
   * same object.
   *
   * Two cases this does NOT cover, stated rather than claimed away. If
   * Transcribe re-decodes the leading word's timestamp between the partial and
   * the final, the two get different ids and the partial is orphaned. And if a
   * result carries no StartTime and no timed item at all, the anchor is 0, so
   * two such results in one session collide -- there is nothing in a result
   * with no times and no id that distinguishes it from another while still
   * matching its own final, so this is a limit of the fallback rather than a
   * choice between designs.
   *
   * Both need `ResultId` to be absent, and Transcribe always sends one, so
   * none of this is reachable through the service. The fallback exists so that
   * a result without an id still lands on a row of its own wherever it can,
   * because the failure mode of a colliding id is a meeting overwriting itself
   * onto one transcript row.
   */
  private static fallbackResultId(result: any, timeOffsetSeconds: number): string {
    const items = result?.Alternatives?.[0]?.Items ?? [];
    const firstTimed = items.find(
      (item: any) => item?.Type === 'pronunciation' && item.StartTime !== undefined,
    );
    const anchor = result?.StartTime ?? firstTimed?.StartTime ?? 0;
    return `vp-${timeOffsetSeconds}-${anchor}`;
  }

  /**
   * The rows for one Transcribe result: one per participant turn, and with
   * voice separation on, one per recovered voice turn within that.
   *
   * Every word is named after the participant the platform showed for most
   * of the word, and the result is cut where that changes. With voice
   * separation on, the voice label is kept as a hint under the participant it
   * mostly coincides with (see VoiceLabelNames).
   *
   * Segment ids are keyed on Transcribe's own `ResultId` plus the piece
   * index, NOT on the speaker name. That is what keeps a partial aligned with
   * its final: a partial is always one piece at index 0 and is cleanly
   * replaced by the final's first piece, while later pieces arrive as new
   * segments. Keying on anything derived from the name would rename the
   * segment between partial and final and orphan the partial, which would then
   * sit in the UI for ever marked in-progress.
   */
  private processTranscriptionResults(speakerName: string, result: any, timeOffsetSeconds = 0): any {
    const items: TranscribeItem[] = result.Alternatives?.[0]?.Items ?? [];
    // See fallbackResultId for what it is keyed on and what it does not cover.
    const resultId =
      result.ResultId ?? KinesisStreamManager.fallbackResultId(result, timeOffsetSeconds);
    if (this.voiceLabelsOffset !== timeOffsetSeconds) {
      this.voiceLabels = new VoiceLabelNames();
      this.voiceLabelsOffset = timeOffsetSeconds;
    }
    const labels = this.voiceLabels;
    const segments = buildDiarizedSegments(items, {
      resultId,
      timeOffsetSeconds,
      rosterNameFor: (start, end) => this.rosterLookup?.(start, end) ?? speakerName,
      nameFor: this.diarize
        ? (roster, label, seconds) => labels.nameFor(roster, label, seconds)
        : (roster) => roster,
      isPartial: KinesisStreamManager.isPartialResult(result),
      useLabels: this.diarize,
    });

    if (diagnosticsOn()) {
      const names = Object.values(segments).map((segment) => segment.Speaker);
      console.log(
        `[DIARIZATION] result=${resultId} partial=${KinesisStreamManager.isPartialResult(result)} ` +
          `items=${items.length} segments=${names.length} speakers=${names.join(',')}`,
      );
    }
    return segments;
  }

  async sendCallCategory(category: string, score?: number): Promise<void> {
    const record: KinesisRecord = {
      eventType: 'ADD_CALL_CATEGORY',
      callId: this.callId,
      timestamp: Date.now(),
      callMetaData: {
        callId: this.callId,
        callCategories: [category],
        callCategoryScore: score,
      },
    };

    await this.sendRecord(record);
  }

  async sendRealTimeAnalytics(metadata: Partial<CallMetaData>): Promise<void> {
    const record: KinesisRecord = {
      eventType: 'ADD_REAL_TIME_CALL_ANALYTICS_METADATA',
      callId: this.callId,
      timestamp: Date.now(),
      callMetaData: {
        callId: this.callId,
        ...metadata,
      },
    };

    await this.sendRecord(record);
  }

  // Batch send multiple records for efficiency
  async sendBatchRecords(records: KinesisRecord[]): Promise<void> {
    if (!this.streamName || records.length === 0) {
      return;
    }

    try {
      const command = new PutRecordsCommand({
        StreamName: this.streamName,
        Records: records.map(record => ({
          Data: Buffer.from(JSON.stringify(record)),
          PartitionKey: this.callId,
        })),
      });

      const result = await this.kinesisClient.send(command);
      console.log(`Kinesis batch sent: ${records.length} records, ${result.FailedRecordCount || 0} failed`);
      
      if (result.FailedRecordCount && result.FailedRecordCount > 0) {
        console.error('Some Kinesis records failed:', result.Records?.filter(r => r.ErrorCode));
      }
    } catch (error) {
      console.error('Failed to send Kinesis batch:', error);
      throw error;
    }
  }

  getCallId(): string {
    return this.callId;
  }
}

// Export singleton instance
export const kinesisStreamManager = new KinesisStreamManager();

// Convenience functions for backward compatibility with LMA Python code
export const sendStartMeeting = () => kinesisStreamManager.sendStartMeeting();
export const sendEndMeeting = (recordingUrl?: string) => kinesisStreamManager.sendEndMeeting(recordingUrl);
export const sendAsrTranscriptSegment = (segment: TranscriptSegment) =>
  kinesisStreamManager.sendAsrTranscriptSegment(segment);
export const sendAddTranscriptSegment = (speaker: string, transcriptResult: any, timeOffsetSeconds = 0) =>
  kinesisStreamManager.sendTranscriptSegment(speaker, transcriptResult, timeOffsetSeconds);
export const sendCallCategory = (category: string, score?: number) => 
  kinesisStreamManager.sendCallCategory(category, score);
export const sendCallRecording = (url: string) => kinesisStreamManager.sendCallRecording(url);
export const sendCallVideoRecording = (url: string) => kinesisStreamManager.sendCallVideoRecording(url);
