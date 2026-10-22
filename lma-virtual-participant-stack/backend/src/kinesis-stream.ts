import { KinesisClient, PutRecordCommand, PutRecordsCommand } from '@aws-sdk/client-kinesis';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { details } from './details.js';
import { speakerNameFor } from './asr-microvm-client.js';
import { buildDiarizedSegments, type TranscribeItem } from './transcribe-diarization.js';

export interface TranscriptSegment {
  channel: string;
  segmentId: string;
  startTime: number;
  endTime: number;
  transcript: string;
  isPartial: boolean;
  speaker?: string;
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

  /** The meeting's CallId, so other services (the ASR launcher lease) can key on it. */
  get currentCallId(): string {
    return this.callId;
  }
  private streamName: string;
  
  // Global speaker tracking (matching Python kds.py)
  private currentSpeakerName: string | null = null;
  private speakers: string[] = [];
  private startTimes: number[] = [];
  // Whether to recover speaker turns from Transcribe's per-item labels. Off
  // unless the deployment's VP voice-separation switch is on, so a deployment
  // that has not asked for it keeps exactly today's attribution.
  private diarize = false;

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

  private async sendRecord(record: any): Promise<void> {
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

  syncTranscriptSegmentState(speaker: string, transcriptResult: any, timeOffsetSeconds = 0): void {
    this.processTranscriptionResults(speaker, transcriptResult, timeOffsetSeconds);
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
  }

  /**
   * The roster name in effect at an absolute time.
   *
   * Reuses the timeline the roster bookkeeping already maintains -- speakers[]
   * paired with startTimes[] -- rather than introducing a second source, so
   * diarized and undiarized segments name the same participant for the same
   * moment.
   */
  private rosterNameAt(absoluteStart: number, fallback: string): string {
    if (this.startTimes.length === 0) {
      return fallback;
    }
    // Upper bound, not the lower bound the undiarized path uses. A roster entry
    // is pushed with the start time of the word at which the change was
    // noticed, so an exact tie means "this speaker from here on". Resolving a
    // tie to the PREVIOUS speaker misattributes only the words at exactly that
    // timestamp -- but because the entry is stamped from the last item of the
    // partial in hand, those are the opening words of the new speaker's turn,
    // which the roster splitter then peels off as a short segment under the
    // previous participant's name. The undiarized path keeps its lower-bound
    // search, because changing a shared helper would alter the behaviour every
    // deployment has today.
    let index = this.upperBound(this.startTimes, absoluteStart) - 1;
    if (index < 0) {
      index = 0;
    }
    return this.speakers[index] ?? fallback;
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
   * One segment per recovered speaker turn, named `roster (spk_N)`.
   *
   * Composes both signals the way the on-demand engine does: the roster says
   * which participant the platform reports, the voice label says which voice
   * within it. For a conference room sharing one speakerphone that is the only
   * thing that separates the people in the room.
   *
   * Segment ids are keyed on Transcribe's own `ResultId` plus the run index,
   * NOT on the speaker name. That is what keeps a partial aligned with its
   * final: a partial is always one piece at index 0 and is cleanly replaced by
   * the final's first piece, while later pieces arrive as new segments. Keying
   * on anything derived from the label would rename the segment between partial
   * and final and orphan the partial, which would then sit in the UI for ever
   * marked in-progress.
   */
  private diarizedSegments(speakerName: string, result: any, timeOffsetSeconds: number): any {
    const items: TranscribeItem[] = result.Alternatives?.[0]?.Items ?? [];
    // See fallbackResultId for what it is keyed on and what it does not cover.
    const resultId =
      result.ResultId ?? KinesisStreamManager.fallbackResultId(result, timeOffsetSeconds);
    const segments = buildDiarizedSegments(items, {
      resultId,
      timeOffsetSeconds,
      rosterNameAt: (startTime) => this.rosterNameAt(startTime, speakerName),
      nameFor: speakerNameFor,
      isPartial: KinesisStreamManager.isPartialResult(result),
    });

    if (diagnosticsOn()) {
      const labels = Object.values(segments).map((segment) => segment.Speaker);
      console.log(
        `[DIARIZATION] result=${resultId} partial=${KinesisStreamManager.isPartialResult(result)} ` +
          `items=${items.length} segments=${labels.length} speakers=${labels.join(',')}`,
      );
    }
    return segments;
  }

  private processTranscriptionResults(speakerName: string, result: any, timeOffsetSeconds = 0): any {
    const segments: any = {};

    if (this.currentSpeakerName !== speakerName) {
      // Start time of new speaker is the start time of the last item in the results.
      // Store absolute (offset-adjusted) time so ids/ordering stay monotonic across
      // reconnects — Transcribe resets Item.StartTime to 0 on every (re)connect.
      this.currentSpeakerName = speakerName;
      this.speakers.push(speakerName);
      const lastItem = result.Alternatives[0].Items[result.Alternatives[0].Items.length - 1];
      this.startTimes.push((lastItem.StartTime ?? 0) + timeOffsetSeconds);
    }

    if (this.diarize) {
      // The roster bookkeeping above still runs, because rosterNameAt reads the
      // timeline it maintains and the non-partial reset below depends on it.
      const diarized = this.diarizedSegments(speakerName, result, timeOffsetSeconds);
      if (!KinesisStreamManager.isPartialResult(result)) {
        this.currentSpeakerName = null;
        this.speakers = [];
        this.startTimes = [];
      }
      return diarized;
    }

    const alternative = result.Alternatives[0];
    for (const item of alternative.Items) {
      this.addItemToSegment(item, segments, timeOffsetSeconds);
      if (process.env.DEBUG) {
        console.log(`DEBUG: Item ${item.StartTime}, ${item.EndTime}, ${item.Content}`);
        console.log(`DEBUG: Speakers ${this.speakers}`);
        console.log(`DEBUG: StartTimes ${this.startTimes}`);
        console.log(`DEBUG: Segments ${JSON.stringify(segments)}`);
      }
    }
    
    // If it's a non partial result, then re-initialize globals
    if (!KinesisStreamManager.isPartialResult(result)) {
      console.log("INFO: Non partial result - Resetting speaker and start times");
      this.currentSpeakerName = null;
      this.speakers = [];
      this.startTimes = [];
    }
    
    return segments;
  }

  private addItemToSegment(item: any, segments: any, timeOffsetSeconds = 0): any {
    // startTimes are already absolute (offset-adjusted); compare against the
    // item's absolute start time so the speaker lookup stays correct.
    const itemStart = (item.StartTime ?? 0) + timeOffsetSeconds;
    const itemEnd = (item.EndTime ?? 0) + timeOffsetSeconds;
    // Find the correct segment index using binary search (matching Python bisect)
    let segmentIndex = this.binarySearch(this.startTimes, itemStart) - 1;
    if (segmentIndex < 0) {
      segmentIndex = 0;
    }

    const segmentId = `${this.speakers[segmentIndex]}-${this.startTimes[segmentIndex]}`;

    if (!(segmentId in segments)) {
      segments[segmentId] = {
        SegmentId: segmentId,
        Speaker: this.speakers[segmentIndex],
        StartTime: this.startTimes[segmentIndex],
        EndTime: itemEnd,
        Transcript: ''
      };
    } else if (item.Type === 'pronunciation') {
      // Add a space between words
      segments[segmentId].Transcript += " ";
    }

    segments[segmentId].EndTime = itemEnd;
    segments[segmentId].Transcript += item.Content;

    return segments;
  }

  /** Index of the first element strictly greater than `target` (bisect_right). */
  private upperBound(arr: number[], target: number): number {
    let left = 0;
    let right = arr.length;
    while (left < right) {
      const mid = Math.floor((left + right) / 2);
      if (arr[mid] <= target) {
        left = mid + 1;
      } else {
        right = mid;
      }
    }
    return left;
  }

  private binarySearch(arr: number[], target: number): number {
    // JavaScript implementation of Python's bisect.bisect_left
    let left = 0;
    let right = arr.length;
    
    while (left < right) {
      const mid = Math.floor((left + right) / 2);
      if (arr[mid] < target) {
        left = mid + 1;
      } else {
        right = mid;
      }
    }
    
    return left;
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
