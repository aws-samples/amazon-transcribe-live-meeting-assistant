import { KinesisClient, PutRecordCommand, PutRecordsCommand } from '@aws-sdk/client-kinesis';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { details } from './details.js';

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

class KinesisStreamManager {
  private kinesisClient: KinesisClient;
  private callId: string;
  private streamName: string;
  
  // Global speaker tracking (matching Python kds.py)
  private currentSpeakerName: string | null = null;
  private speakers: string[] = [];
  private startTimes: number[] = [];

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

  private async linkCallToVP(vpId: string, callId: string): Promise<void> {
    /**Link CallId to Virtual Participant record via GraphQL (matching Python implementation with SigV4)*/
    try {
        const graphqlEndpoint = details.graphqlEndpoint;
        if (!graphqlEndpoint) {
            console.log('Warning: GRAPHQL_ENDPOINT not set, cannot link CallId');
            return;
        }

        // Use AWS SigV4 signing (matching Python SigV4Auth)
        const { createSignedFetcher } = await import('aws-sigv4-fetch');
        const signedFetch = createSignedFetcher({
            service: 'appsync',
            region: process.env.AWS_REGION || 'us-east-1',
        });

        // First, get the current VP to preserve existing status
        const getQuery = `
            query GetVirtualParticipant($id: ID!) {
                getVirtualParticipant(id: $id) {
                    id
                    status
                }
            }
        `;

        const getResponse = await signedFetch(graphqlEndpoint, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                query: getQuery,
                variables: { id: vpId }
            }),
        });

        if (!getResponse.ok) {
            throw new Error(`Failed to get VP data: HTTP ${getResponse.status} - ${await getResponse.text()}`);
        }

        const getData = await getResponse.json();
        if (getData.errors) {
            throw new Error(`GraphQL errors getting VP: ${JSON.stringify(getData.errors)}`);
        }

        const currentVP = getData.data?.getVirtualParticipant;
        if (!currentVP) {
            throw new Error(`VP ${vpId} not found`);
        }

        const currentStatus = currentVP.status || 'INITIALIZING';
        console.log(`Current VP status: ${currentStatus}`);

        // Now update with CallId while preserving status
        const mutation = `
            mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
                updateVirtualParticipant(input: $input) {
                    id
                    status
                    CallId
                    updatedAt
                }
            }
        `;

        const variables = {
            input: {
                id: vpId,
                status: currentStatus,
                CallId: callId
            }
        };

        const response = await signedFetch(graphqlEndpoint, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                query: mutation,
                variables: variables
            }),
        });

        if (!response.ok) {
            throw new Error(`Failed to link CallId: HTTP ${response.status} - ${await response.text()}`);
        }

        const responseData = await response.json();
        if (responseData.errors) {
            throw new Error(`GraphQL errors linking CallId: ${JSON.stringify(responseData.errors)}`);
        }

        console.log(`Successfully linked CallId ${callId} to VP ${vpId} with status ${currentStatus}`);
    } catch (error) {
        console.error(`Error linking CallId to VP: ${error}`);
        throw error;
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

  async sendTranscriptSegment(speaker: string, transcriptResult: any): Promise<void> {
    console.log("Process speaker changes to identify segments within result");
    const segments = this.processTranscriptionResults(speaker, transcriptResult);
    
    for (const segment of Object.values(segments)) {
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
          IsPartial: transcriptResult.IsPartial || false,
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
  }

  private processTranscriptionResults(speakerName: string, result: any): any {
    const segments: any = {};
    
    if (this.currentSpeakerName !== speakerName) {
      // Start time of new speaker is the start time of the last item in the results
      this.currentSpeakerName = speakerName;
      this.speakers.push(speakerName);
      const lastItem = result.Alternatives[0].Items[result.Alternatives[0].Items.length - 1];
      this.startTimes.push(lastItem.StartTime);
    }
    
    const alternative = result.Alternatives[0];
    for (const item of alternative.Items) {
      this.addItemToSegment(item, segments);
      if (process.env.DEBUG) {
        console.log(`DEBUG: Item ${item.StartTime}, ${item.EndTime}, ${item.Content}`);
        console.log(`DEBUG: Speakers ${this.speakers}`);
        console.log(`DEBUG: StartTimes ${this.startTimes}`);
        console.log(`DEBUG: Segments ${JSON.stringify(segments)}`);
      }
    }
    
    // If it's a non partial result, then re-initialize globals
    if (!result.IsPartial) {
      console.log("INFO: Non partial result - Resetting speaker and start times");
      this.currentSpeakerName = null;
      this.speakers = [];
      this.startTimes = [];
    }
    
    return segments;
  }

  private addItemToSegment(item: any, segments: any): any {
    // Find the correct segment index using binary search (matching Python bisect)
    let segmentIndex = this.binarySearch(this.startTimes, item.StartTime) - 1;
    if (segmentIndex < 0) {
      segmentIndex = 0;
    }
    
    const segmentId = `${this.speakers[segmentIndex]}-${this.startTimes[segmentIndex]}`;
    
    if (!(segmentId in segments)) {
      segments[segmentId] = {
        SegmentId: segmentId,
        Speaker: this.speakers[segmentIndex],
        StartTime: this.startTimes[segmentIndex],
        EndTime: item.EndTime,
        Transcript: ''
      };
    } else if (item.Type === 'pronunciation') {
      // Add a space between words
      segments[segmentId].Transcript += " ";
    }
    
    segments[segmentId].EndTime = item.EndTime;
    segments[segmentId].Transcript += item.Content;
    
    return segments;
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
export const sendAddTranscriptSegment = (speaker: string, transcriptResult: any) => 
  kinesisStreamManager.sendTranscriptSegment(speaker, transcriptResult);
export const sendCallCategory = (category: string, score?: number) => 
  kinesisStreamManager.sendCallCategory(category, score);
export const sendCallRecording = (url: string) => kinesisStreamManager.sendCallRecording(url);
