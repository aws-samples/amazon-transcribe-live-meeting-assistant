import { v4 as uuidv4 } from 'uuid';
import { normalizeChatText } from './chat-text.js';

export interface MeetingInvite {
  meetingId: string;
  meetingPassword?: string;
  meetingPlatform: 'Chime' | 'Zoom' | 'Teams' | 'Webex' | 'CHIME' | 'ZOOM' | 'TEAMS' | 'WEBEX';
  meetingName: string;
  meetingTime: number;
  scheduledFor?: string;
  isScheduled?: boolean;
  scheduleId?: string;
  userName: string;
  virtualParticipantId?: string;
}

export interface Speaker {
  name: string;
  timestamp: number;
}

/**
 * Options passed to each platform handler's initialize(). `prepareAvatar`
 * brings up the Simli avatar (background render page + getUserMedia override
 * + relay wiring) on demand. Handlers call it at the point where the avatar
 * is about to be needed as the camera — for Zoom that's after sign-in and
 * before the prejoin camera toggle, keeping the avatar's CPU load off the
 * sign-in phase. Idempotent; a no-op when Simli is disabled.
 */
export interface MeetingInitOptions {
  prepareAvatar?: () => Promise<void>;
}

export interface MeetingDetails {
  // Meeting Configuration
  invite: MeetingInvite;

  zoomMethod: 'dom' | 'sdk';
  teamsMethod: 'dom' | 'sdk';

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
  domSelectorCacheTableName: string;
  bedrockDomResolverModelId: string;
  
  // Transcription Configuration
  transcribeLanguageCode: string;
  transcribeLanguageOptions: string;
  transcribePreferredLanguage: string;
  enableContentRedaction: boolean;
  transcribeContentRedactionType: string;
  customVocabularyName: string;
  
  // Recording Configuration
  enableAudioRecording: boolean;
  enableVideoRecording: boolean;
  tmpRecordingFilename: string;
  meetingMode?: string;
}

class DetailsManager {
  private _details: MeetingDetails;

  constructor() {
    // Initialize from environment variables
    const meetingPlatform = (process.env.MEETING_PLATFORM as 'Chime' | 'Zoom' | 'Teams' | 'Webex' | 'CHIME' | 'ZOOM' | 'TEAMS' | 'WEBEX') || 'Chime';
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
      'Hello. I am an AI Live Meeting Assistant (LMA). I was invited by {LMA_USER} to join this call. ' +
      'Anyone here can ask me to leave at any time by typing "LMA leave" (or "LMA end") in chat. ' +
      'To learn more please visit: https://amazon.com/live-meeting-assistant.'
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

    const zoomSdkCredsPresent = !!((process.env.ZOOM_MEETING_SDK_CLIENT_ID || '').trim() && (process.env.ZOOM_MEETING_SDK_CLIENT_SECRET || '').trim());
    const zoomMethod = resolveJoinMethod(process.env.MEETING_ZOOM_METHOD, zoomSdkCredsPresent);

    const acsConnectionStringPresent = !!(process.env.ACS_CONNECTION_STRING || '').trim();
    const teamsMethod = resolveJoinMethod(process.env.MEETING_TEAMS_METHOD, acsConnectionStringPresent);

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

      zoomMethod,
      teamsMethod,

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

      // Commands. The end-command matcher (matchesEndCommand below)
      // requires an explicit "LMA" prefix on every dismissal phrase
      // ("LMA END", "LMA LEAVE", "LMA STOP", "LMA QUIT", "Goodbye LMA",
      // etc., case-insensitive, word-bounded) so that prose like
      // "the meeting will end at 3pm" or "I'll leave at 3" never trips
      // a false dismissal. The pause and resume commands are matched by
      // matchesChatCommand: the whole message, case-insensitive, with or
      // without the "LMA" addressee.
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
      domSelectorCacheTableName: process.env.DOM_SELECTOR_CACHE_TABLE_NAME || '',
      bedrockDomResolverModelId: process.env.BEDROCK_DOM_RESOLVER_MODEL_ID || '',

      // Transcription Configuration
      transcribeLanguageCode: process.env.TRANSCRIBE_LANGUAGE_CODE || 'en-US',
      transcribeLanguageOptions: process.env.TRANSCRIBE_LANGUAGE_OPTIONS || '',
      transcribePreferredLanguage: process.env.TRANSCRIBE_PREFERRED_LANGUAGE || '',
      enableContentRedaction: process.env.ENABLE_CONTENT_REDACTION === 'true',
      transcribeContentRedactionType: process.env.TRANSCRIBE_CONTENT_REDACTION_TYPE || 'PII',
      customVocabularyName: process.env.CUSTOM_VOCABULARY_NAME || '',

      // Recording Configuration
      enableAudioRecording: process.env.ENABLE_AUDIO_RECORDING !== 'false',
      // Video recording defaults ON; the per-VP UI toggle and task-definition
      // default set ENABLE_VIDEO_RECORDING='false' to opt out.
      enableVideoRecording: process.env.ENABLE_VIDEO_RECORDING !== 'false',
      tmpRecordingFilename: `/tmp/${meetingName.replace(/[^a-zA-Z0-9]/g, '_')}_${Date.now()}.wav`,
    };
  }

  get details(): MeetingDetails {
    return this._details;
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

export function resolveJoinMethod(override: string | undefined, credentialsPresent: boolean): 'dom' | 'sdk' {
  const mode = (override || 'auto').toLowerCase();
  if (mode === 'sdk') return 'sdk';
  if (mode === 'dom') return 'dom';
  return credentialsPresent ? 'sdk' : 'dom';
}

/**
 * Returns true when a chat message is a direct, two-token dismissal of LMA.
 * The matcher is deliberately strict — it accepts only messages that consist
 * of exactly the addressee + verb (or verb + addressee), with optional
 * lightweight punctuation. This prevents false positives from prose that
 * happens to contain both "LMA" and a dismissal verb — most importantly
 * the bot's own intro message (which itself reads
 *   '...typing "LMA leave" (or "LMA end") in chat.'
 * ), so a second LMA bot in the same meeting can no longer end the first
 * one with its join announcement.
 *
 * Recognised verbs (case-insensitive): end, leave, stop, quit, exit, goodbye, bye.
 * The addressee may be "LMA" or "@LMA".
 *
 * Examples that match:
 *   "LMA end", "LMA, leave!", "@LMA stop", "lma quit",
 *   "Goodbye LMA", "bye, LMA!", "exit LMA"
 *
 * Examples that do NOT match:
 *   "the meeting will end at 3pm", "I have to leave, but LMA looks great",
 *   any message that quotes the command in a longer sentence (including the
 *   bot's own intro), "Hello LMA", "endpoint", "ending soon".
 */
export function matchesEndCommand(message: string): boolean {
  if (!message) return false;
  const trimmed = message.trim();
  if (!trimmed) return false;
  // Cheap belt-and-braces guard — every legitimate command fits comfortably
  // under this. Real prose virtually never does.
  if (trimmed.length > 40) return false;
  const verb = '(?:end|leave|stop|quit|exit|goodbye|bye)';
  const addressee = '@?lma';
  // Allow optional inline punctuation between the two tokens (",", ":", "-",
  // "!", "?", ".") and trailing terminators.
  const sep = '[\\s,:!?.\\-]+';
  const trail = '[\\s!?.]*';
  const addresseeFirst = new RegExp(`^${addressee}${sep}${verb}${trail}$`, 'i');
  const verbFirst = new RegExp(`^${verb}${sep}${addressee}${trail}$`, 'i');
  return addresseeFirst.test(trimmed) || verbFirst.test(trimmed);
}

/**
 * Returns true when a chat message is exactly `command` — the pause
 * (`details.pauseCommand`) or resume (`details.startCommand`) command — and
 * nothing else, optionally addressed to LMA.
 *
 * Pass the message body only, with any sender or recipient prefix the platform
 * adds already removed: the same string each platform hands to
 * matchesEndCommand.
 *
 * Matched case-insensitively and as the whole message, like the dismissal and
 * assistant-mode commands, so a message that merely contains the word — "PAUSED",
 * "please don't PAUSE", an introduction message that explains the command —
 * is ordinary chat. Punctuation is ignored, through the same normalisation as
 * the assistant-mode commands (normalizeChatText), so a one-word reply such as
 * "Pause?" or "start..." counts as the command, as "LMA translate?" does.
 *
 * Examples that match (for "PAUSE"):
 *   "PAUSE", "pause", "Pause.", "Pause?", "LMA pause", "@LMA, PAUSE!", "pause LMA"
 *
 * Examples that do NOT match:
 *   "PAUSED", "please PAUSE", "Type PAUSE to stop the transcript", "LMA", ""
 */
export function matchesChatCommand(message: string, command: string): boolean {
  if (!message || !command) return false;
  const expected = normalizeChatText(command);
  if (!expected) return false;
  const tokens = normalizeChatText(message).split(' ').filter((token) => token.length > 0);
  const addressee = tokens.indexOf('lma');
  const remainder = addressee === -1 ? tokens : [...tokens.slice(0, addressee), ...tokens.slice(addressee + 1)];
  return remainder.join(' ') === expected;
}

/**
 * Canonical reason codes describing why the VP left a meeting. Each platform
 * handler returns one of these; the orchestrator uses it to emit a canonical
 * log line and to derive a human-readable status message persisted to the
 * VP record (visible in the UI).
 */
export type ExitReasonCode =
  | 'end-command'         // attendee typed "LMA leave"/"LMA end"/etc. in chat
  | 'alone-in-meeting'    // VP is the only attendee left
  | 'removed-from-meeting'// host kicked the VP, or VP removed by platform
  | 'host-ended'          // host ended the meeting for everyone
  | 'meeting-timeout'     // VP hit the configured maximum meeting duration
  | 'page-closed'         // the browser/page went away unexpectedly
  | 'never-joined'        // VP never actually entered the meeting (prejoin
                          // timeout, never admitted, stuck on join form) —
                          // this is a FAILURE, not a normal completion
  | 'unknown';            // fallback when no signal could be classified

/**
 * True when the VP actually made it into the meeting, so ending is a normal
 * COMPLETED. False for reasons that mean it never got in — those are surfaced
 * as FAILED instead, so the UI does not report success for a meeting the VP
 * never joined.
 *
 * Written as an exhaustive switch rather than `reason !== 'never-joined'` so
 * that adding a reason code forces a decision here: a new code that nobody
 * classified is a compile error, not a silent "joined".
 *
 * `unknown` counts as joined, and that is a deliberate choice rather than
 * inherited behaviour. Every place that produces it sits past the handler's
 * admission gate — the initial value of `exitInfo` while watching for the
 * meeting to end, and the catch around that watch (see `webex.ts`, `chime.ts`,
 * `zoom.ts`, `teams.ts`). So `unknown` means "the VP was in the meeting and we
 * could not classify why it left", and scoring it as a failure would report
 * FAILED for meetings that ran and produced a transcript. Not being admitted
 * is reported by its own code, `never-joined`, from the pre-join path; the
 * pre-join paths must never return `unknown`.
 *
 * Four of the five are initialisers that cannot survive: every racer and every
 * catch in `chime.ts`, `zoom.ts` and `teams.ts` assigns a concrete reason
 * before the value is returned, and `chime.ts` has no catch at all, so an
 * exception there propagates rather than returning. The one `unknown` that can
 * actually reach the orchestrator is `webex.ts`'s catch around its own wait.
 * `exit-reason.test.ts` holds that count to four and asserts each one is still
 * a wait initialiser, so this paragraph cannot quietly go out of date.
 */
export function didJoinMeeting(info: ExitInfo): boolean {
  switch (info.reason) {
    case 'never-joined':
      return false;
    case 'end-command':
    case 'alone-in-meeting':
    case 'removed-from-meeting':
    case 'host-ended':
    case 'meeting-timeout':
    case 'page-closed':
    case 'unknown':
      return true;
    default: {
      // Exhaustiveness: a new ExitReasonCode fails to compile here until it is
      // classified above.
      const unclassified: never = info.reason;
      return unclassified;
    }
  }
}

/** Longest trigger persisted. Every trigger in the taxonomy is far shorter. */
export const MAX_EXIT_TRIGGER_LENGTH = 100;

/**
 * Reduce an exit trigger to something that can be counted, before it is
 * persisted on the VP record.
 *
 * Nearly every trigger is a fixed literal — 'pre-join:not-admitted',
 * 'attendees-left', 'ZOOM_END_DIALOG' — and passes through untouched, which is
 * the point: the value is only worth persisting if rows sharing a cause share a
 * string. A few interpolate a platform error message instead ('error:…',
 * 'poll-error:…'), and a Playwright message is long, multi-line and close to
 * unique, so persisting it verbatim would leave those rows outside every
 * aggregate and make the attribute a second, worse copy of `errorMessage` —
 * which is where that prose belongs and already goes.
 *
 * So: the segment before the first colon — the part that classifies — is kept
 * exactly, and everything after it is reduced to a slug, every time rather than
 * only when the result is long. Doing it only to long triggers would leave a
 * short platform message stored verbatim, and two reports of one failure
 * differing by a word or a full stop would not group, which is the whole
 * property this field is for. Playwright messages begin with their stable part
 * ('Target closed', 'Execution context was destroyed'), so the slug still
 * distinguishes causes.
 *
 * A trigger with no colon is left alone apart from the length cap. Those are
 * the fixed ones — 'ZOOM_END_DIALOG', 'HANGUP_BUTTON_HIDDEN', 'attendees-left'
 * — which are already one value per cause, and slugging them would only make
 * them harder to recognise.
 *
 * Which is why a trigger that carries a detail puts it after a colon: that is
 * the convention this function relies on, and `exit-reason.test.ts` holds the
 * handlers to it rather than trusting them.
 */
export function normalizeExitTrigger(trigger?: string): string | undefined {
  if (!trigger) return undefined;
  const oneLine = trigger.replace(/\s+/g, ' ').trim();
  if (!oneLine) return undefined;
  const colon = oneLine.indexOf(':');
  if (colon < 0) return oneLine.slice(0, MAX_EXIT_TRIGGER_LENGTH);
  const prefix = oneLine.slice(0, colon);
  const slug = oneLine
    .slice(colon + 1)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  // The trailing hyphen trim matters for more than looks: without it the cap
  // can cut mid-word and leave one, and normalising the result again would
  // remove it, so the function would not be idempotent.
  return `${prefix}:${slug}`.slice(0, MAX_EXIT_TRIGGER_LENGTH).replace(/-+$/, '');
}

export interface ExitInfo {
  reason: ExitReasonCode;
  /** Platform-specific identifier of the exact branch (e.g. 'ZOOM_END_DIALOG',
   *  'HANGUP_BUTTON_HIDDEN', 'attendees-left'). Logged but not displayed. */
  trigger?: string;
  /** When reason==='end-command', the parsed sender name (when available). */
  requestedBy?: string | null;
  /** When reason==='end-command', the chat-message body that matched. */
  matchedMessage?: string;
}

/**
 * Build the human-readable status detail shown in the UI alongside
 * COMPLETED. Keep these short and user-facing — the canonical reason code
 * and trigger live in the logs, not in this string.
 */
export function formatExitMessage(info: ExitInfo): string {
  switch (info.reason) {
    case 'end-command':
      return info.requestedBy
        ? `Asked to leave by ${info.requestedBy}.`
        : 'Asked to leave by a participant.';
    case 'alone-in-meeting':
      return 'Everyone else left the meeting.';
    case 'removed-from-meeting':
      return 'Removed from the meeting.';
    case 'host-ended':
      return 'Meeting ended by host.';
    case 'meeting-timeout':
      return 'Meeting reached maximum duration.';
    case 'page-closed':
      return 'Meeting page closed unexpectedly.';
    case 'never-joined':
      return 'Could not join the meeting (was not admitted, or stuck on the join screen).';
    case 'unknown':
    default:
      return 'Meeting ended.';
  }
}

/**
 * Build a personalised exit-message list. When we know who issued the
 * dismissal, the goodbye acknowledges them so other participants understand
 * what just happened.
 */
export function exitMessagesFor(requester?: string | null): string[] {
  if (!requester) return details.exitMessages;
  const safe = requester.replace(/[\r\n\t]+/g, ' ').trim().slice(0, 80);
  if (!safe) return details.exitMessages;
  // Prepend an acknowledgement; keep the configured exit message as the
  // farewell so users keeping a custom exit message still see it.
  return [`Thanks ${safe} — I'll head out now.`, ...details.exitMessages];
}
