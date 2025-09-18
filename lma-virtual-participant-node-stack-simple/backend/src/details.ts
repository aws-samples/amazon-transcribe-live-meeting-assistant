import { v4 as uuidv4 } from 'uuid';

export interface MeetingInvite {
  meetingId: string;
  meetingPassword?: string;
  meetingPlatform: 'Chime' | 'Zoom' | 'Webex' | 'CHIME' | 'ZOOM' | 'WEBEX';
  meetingName: string;
  meetingTime: number;
  userName: string;
  virtualParticipantId?: string;
}

export interface Speaker {
  name: string;
  timestamp: number;
}

export interface MeetingDetails {
  // Meeting Configuration
  invite: MeetingInvite;
  
  // LMA Configuration
  lmaIdentity: string;
  lmaUser: string;
  
  // Meeting State
  start: boolean;
  speakers: Speaker[];
  messages: string[];
  captions: string[];
  attachments: Record<string, string>;
  
  // Meeting Control Messages
  introMessages: string[];
  startMessages: string[];
  pauseMessages: string[];
  exitMessages: string[];
  
  // Commands
  startCommand: string;
  pauseCommand: string;
  endCommand: string;
  
  // Timeouts
  waitingTimeout: number;
  meetingTimeout: number;
  
  // Scribe Identity
  scribeIdentity: string;
  scribeName: string;
  
  // LMA Integration
  callDataStreamName: string;
  recordingsBucketName: string;
  recordingsKeyPrefix: string;
  graphqlEndpoint: string;
  vpTaskRegistryTableName: string;
  
  // Transcription Configuration
  transcribeLanguageCode: string;
  enableContentRedaction: boolean;
  transcribeContentRedactionType: string;
  customVocabularyName: string;
  
  // Recording Configuration
  enableAudioRecording: boolean;
  tmpRecordingFilename: string;
  
  // Status Management
  updateInvite: (status: string) => Promise<void>;
  deleteInvite: () => Promise<void>;
}

class DetailsManager {
  private _details: MeetingDetails;

  constructor() {
    // Initialize from environment variables
    const meetingPlatform = (process.env.MEETING_PLATFORM as 'Chime' | 'Zoom' | 'Webex' | 'CHIME' | 'ZOOM' | 'WEBEX') || 'Chime';
    const meetingId = process.env.MEETING_ID || '';
    const meetingPassword = process.env.MEETING_PASSWORD || '';
    const meetingName = process.env.MEETING_NAME || 'LMA Meeting';
    const meetingTime = parseInt(process.env.MEETING_TIME || '0');
    const userName = process.env.LMA_USER || 'LMA User';
    const virtualParticipantId = process.env.VIRTUAL_PARTICIPANT_ID || uuidv4();

    // LMA Configuration
    const lmaIdentity = process.env.LMA_IDENTITY || 'LMA ({LMA_USER})';
    const lmaUser = userName;

    // Replace {LMA_USER} placeholder in messages
    const replacePlaceholders = (message: string): string => {
      return message.replace(/{LMA_USER}/g, lmaUser);
    };

    // Messages Configuration
    const introMessage = replacePlaceholders(
      process.env.INTRO_MESSAGE || 
      'Hello. I am an AI Live Meeting Assistant (LMA). I was invited by {LMA_USER} to join this call. To learn more about me please visit: https://amazon.com/live-meeting-assistant.'
    );
    const startRecordingMessage = replacePlaceholders(
      process.env.START_RECORDING_MESSAGE || 'Live Meeting Assistant started.'
    );
    const stopRecordingMessage = replacePlaceholders(
      process.env.STOP_RECORDING_MESSAGE || 'Live Meeting Assistant stopped.'
    );
    const exitMessage = replacePlaceholders(
      process.env.EXIT_MESSAGE || 'Live Meeting Assistant has left the room.'
    );

    this._details = {
      // Meeting Configuration
      invite: {
        meetingId,
        meetingPassword: meetingPassword || undefined,
        meetingPlatform,
        meetingName,
        meetingTime,
        userName,
        virtualParticipantId,
      },

      // LMA Configuration
      lmaIdentity: replacePlaceholders(lmaIdentity),
      lmaUser,

      // Meeting State
      start: true, // Start transcription by default (LMA behavior)
      speakers: [],
      messages: [],
      captions: [],
      attachments: {},

      // Meeting Control Messages
      introMessages: [introMessage],
      startMessages: [startRecordingMessage],
      pauseMessages: [stopRecordingMessage],
      exitMessages: [exitMessage],

      // Commands
      startCommand: 'START',
      pauseCommand: 'PAUSE',
      endCommand: 'END',

      // Timeouts
      waitingTimeout: 300000, // 5 minutes
      meetingTimeout: 14400000, // 4 hours

      // Scribe Identity
      scribeIdentity: replacePlaceholders(lmaIdentity),
      scribeName: 'LMA',

      // LMA Integration
      callDataStreamName: process.env.CALL_DATA_STREAM_NAME || '',
      recordingsBucketName: process.env.RECORDINGS_BUCKET_NAME || '',
      recordingsKeyPrefix: process.env.RECORDINGS_KEY_PREFIX || 'lma-audio-recordings/',
      graphqlEndpoint: process.env.GRAPHQL_ENDPOINT || '',
      vpTaskRegistryTableName: process.env.VP_TASK_REGISTRY_TABLE_NAME || '',

      // Transcription Configuration
      transcribeLanguageCode: process.env.TRANSCRIBE_LANGUAGE_CODE || 'en-US',
      enableContentRedaction: process.env.ENABLE_CONTENT_REDACTION === 'true',
      transcribeContentRedactionType: process.env.TRANSCRIBE_CONTENT_REDACTION_TYPE || 'PII',
      customVocabularyName: process.env.CUSTOM_VOCABULARY_NAME || '',

      // Recording Configuration
      enableAudioRecording: process.env.ENABLE_AUDIO_RECORDING !== 'false',
      tmpRecordingFilename: `/tmp/${meetingName.replace(/[^a-zA-Z0-9]/g, '_')}_${Date.now()}.wav`,

      // Status Management Functions
      updateInvite: this.updateInviteStatus.bind(this),
      deleteInvite: this.deleteInviteRecord.bind(this),
    };
  }

  get details(): MeetingDetails {
    return this._details;
  }

  private async updateInviteStatus(status: string): Promise<void> {
    try {
      // Update status in DynamoDB VP Task Registry (LMA integration)
      if (this._details.vpTaskRegistryTableName && this._details.invite.virtualParticipantId) {
        console.log(`Updating VP status to: ${status} for ID: ${this._details.invite.virtualParticipantId}`);
        // TODO: Implement DynamoDB update
      }

      // Update status via GraphQL (if endpoint available)
      if (this._details.graphqlEndpoint) {
        console.log(`Updating meeting status via GraphQL: ${status}`);
        // TODO: Implement GraphQL mutation
      }
    } catch (error) {
      console.error('Failed to update invite status:', error);
    }
  }

  private async deleteInviteRecord(): Promise<void> {
    try {
      // Clean up VP Task Registry record
      if (this._details.vpTaskRegistryTableName && this._details.invite.virtualParticipantId) {
        console.log(`Deleting VP record for ID: ${this._details.invite.virtualParticipantId}`);
        // TODO: Implement DynamoDB delete
      }
    } catch (error) {
      console.error('Failed to delete invite record:', error);
    }
  }

  // Utility method to get meeting name with timestamp (for file naming)
  getMeetingNameWithTimestamp(): string {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    return `${this._details.invite.meetingName}_${timestamp}`;
  }

  // Utility method to format filename for recordings
  getRecordingFilename(): string {
    const safeName = this.getMeetingNameWithTimestamp().replace(/[^a-zA-Z0-9_-]/g, '_');
    return `${safeName}.wav`;
  }
}

// Export singleton instance
export const detailsManager = new DetailsManager();
export const details = detailsManager.details;
