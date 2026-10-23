/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Changing how the voice assistant behaves while the meeting is running.
 *
 * Two things about the assistant are worth changing mid-meeting, and until now
 * both were fixed for the life of the Virtual Participant: the meeting mode
 * (`normal` assistant / `group` listening / `translator`), which came from the
 * Nova Sonic configuration record and was read once at task start, and the
 * activation mode (`always_active` / `wake_phrase`), which came from a stack
 * parameter. Changing either meant editing configuration and starting a new
 * meeting.
 *
 * This module holds the parts that every route into that change can share: the
 * request and outcome shapes, the chat vocabulary, the confirmation wording, and
 * the single log line each toggle emits. Routes differ only in how they arrive
 * at a request — chat today, meeting-page buttons, voice phrases and a GraphQL
 * mutation next — so a new route supplies an `AssistantToggleRoute` and reuses
 * everything below.
 *
 * Nothing here is persisted. A toggle changes the assistant running this one
 * meeting and lasts until the meeting ends; the configuration record keeps
 * holding the deployment's default. That is deliberate — a participant switching
 * their own meeting into translator mode should not change what the next meeting
 * starts as — and it is why this feature adds no configuration field.
 */

// Type-only on both of these. `voice-assistant-interface.ts` imports the request
// and outcome shapes back from here, and the meeting-mode union lives beside the
// configuration loader that reads it — erasing both imports keeps this module
// free of a runtime cycle and of the DynamoDB client that loader pulls in.
import type { ActivationMode, VoiceAssistantProvider } from './voice-assistant-interface.js';
import type { MeetingMode } from './nova-sonic-config-loader.js';
import { details } from './details.js';

/** Which surface asked for the change. Recorded on every toggle so a live test
 *  can tell a chat command from a button press from a voice phrase. */
export type AssistantToggleRoute = 'chat' | 'voice' | 'api' | 'config';

export interface AssistantReconfigureRequest {
    /** Meeting mode to switch to. Omitted leaves the current mode alone. */
    meetingMode?: MeetingMode;
    /** Activation mode to switch to. Omitted leaves the current mode alone. */
    activationMode?: ActivationMode;
    /** Which surface the request came from. */
    route: AssistantToggleRoute;
    /** Who asked, when the route knows (a chat sender name, a signed-in user). */
    requestedBy?: string | null;
}

/**
 * How a request ended.
 *
 * `unchanged` and `declined` are kept apart deliberately: the first means the
 * assistant was already doing what was asked and the request cost the meeting
 * nothing, and the second means it was asked for something it would not or could
 * not do. Those read very differently in a log and in a chat reply.
 */
export type AssistantToggleResult = 'applied' | 'unchanged' | 'declined';

export interface AssistantReconfigureOutcome {
    result: AssistantToggleResult;
    /** The fields that changed, from `['meetingMode', 'activationMode']`. Empty
     *  unless `result` is `applied`. */
    changed: Array<'meetingMode' | 'activationMode'>;
    /** Whether the provider tore down and rebuilt its model session. */
    sessionRebuilt: boolean;
    meetingModeBefore: MeetingMode;
    meetingModeAfter: MeetingMode;
    activationModeBefore: ActivationMode;
    activationModeAfter: ActivationMode;
    /** Why nothing was applied. Set whenever `result` is not `applied`. */
    detail?: string;
}

const MEETING_MODES: ReadonlySet<string> = new Set<MeetingMode>(['normal', 'group', 'translator']);
const ACTIVATION_MODES: ReadonlySet<string> = new Set<ActivationMode>([
    'always_active',
    'wake_phrase',
    'strands_tool',
]);

export function isMeetingMode(value: unknown): value is MeetingMode {
    return typeof value === 'string' && MEETING_MODES.has(value);
}

export function isActivationMode(value: unknown): value is ActivationMode {
    return typeof value === 'string' && ACTIVATION_MODES.has(value);
}

/**
 * Publish the meeting mode onto the shared meeting details, where the
 * transcription path reads it.
 *
 * `scribe.ts` consults `details.meetingMode` in two places, and both have to see
 * the mode the assistant is actually in rather than the mode it started in. On
 * the Amazon Transcribe path it is read for every transcript result, to keep the
 * assistant's own translations out of a translator meeting's transcript — so a
 * stale value there either lets translations through or, worse, goes on dropping
 * real segments after the meeting has left translator mode. On the MicroVM ASR
 * path it is read once per engine run, to decide whether to open a transcript
 * channel for the assistant at all.
 *
 * Both the configuration loader and a runtime toggle go through here, so there is
 * one place to change when `NovaSonicConfigTable` moves into a unified
 * configuration store.
 */
export function publishMeetingMode(mode: MeetingMode): void {
    details.meetingMode = mode;
}

// ---------------------------------------------------------------------------
// The chat vocabulary
// ---------------------------------------------------------------------------

/**
 * What a recognised chat command asks for. The same two fields every route
 * supplies, so a command can be handed to `reconfigure` with only the route and
 * the requester added.
 */
export interface AssistantChatCommand {
    meetingMode?: MeetingMode;
    activationMode?: ActivationMode;
}

/**
 * The chat vocabulary, as whole-message phrases rather than keywords.
 *
 * Every entry is the complete message once the `LMA` addressee is removed, so a
 * message only matches when it consists of nothing but the addressee and one of
 * these phrases. Prose always leaves words over and so cannot match — the same
 * property `isStopTalkingRequest` relies on, and a stronger one than a keyword
 * search, which is why "the LMA group is meeting at 3" does not switch the
 * assistant into group mode.
 *
 * The phrasings are the ones people reach for first, plus the enum value itself
 * for anyone who has read the configuration page. `translate` is paired with
 * `assist` rather than with `normal` because the pair has to read as two jobs
 * the assistant can do, not as one mode and the absence of it.
 *
 * `translate` also sets `always_active`. Translator mode speaks on every
 * completed utterance, so on `wake_phrase` activation it receives no audio until
 * somebody says the wake phrase and translates nothing in between; asking for a
 * translator and getting silence would be the more surprising reading of the
 * command. Switching back with `assist` leaves activation where translator put
 * it, because the command carries no information about what it was before —
 * `LMA wake` is how you get the wake phrase back.
 */
const CHAT_COMMANDS: ReadonlyArray<readonly [string, AssistantChatCommand]> = [
    // Meeting mode — what job the assistant is doing.
    ['translate', { meetingMode: 'translator', activationMode: 'always_active' }],
    ['translator', { meetingMode: 'translator', activationMode: 'always_active' }],
    ['translate mode', { meetingMode: 'translator', activationMode: 'always_active' }],
    ['translator mode', { meetingMode: 'translator', activationMode: 'always_active' }],
    ['interpret', { meetingMode: 'translator', activationMode: 'always_active' }],
    ['assist', { meetingMode: 'normal' }],
    ['assistant', { meetingMode: 'normal' }],
    ['assist mode', { meetingMode: 'normal' }],
    ['assistant mode', { meetingMode: 'normal' }],
    ['normal', { meetingMode: 'normal' }],
    ['normal mode', { meetingMode: 'normal' }],
    ['group', { meetingMode: 'group' }],
    ['group mode', { meetingMode: 'group' }],
    // Activation mode — when the assistant is listening.
    ['always', { activationMode: 'always_active' }],
    ['always on', { activationMode: 'always_active' }],
    ['always active', { activationMode: 'always_active' }],
    ['always listen', { activationMode: 'always_active' }],
    ['open mic', { activationMode: 'always_active' }],
    ['wake', { activationMode: 'wake_phrase' }],
    ['wake word', { activationMode: 'wake_phrase' }],
    ['wake phrase', { activationMode: 'wake_phrase' }],
    ['on request', { activationMode: 'wake_phrase' }],
];

const CHAT_COMMAND_INDEX: ReadonlyMap<string, AssistantChatCommand> = new Map(CHAT_COMMANDS);

/** Lowercases and reduces everything that is not a letter or digit to a single
 *  space, so "@LMA, translate!" and "lma translate" arrive here the same.
 *  Matches the normalisation in stop-request.ts. */
function normalize(text: string): string {
    return text
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

/**
 * Whether `message` is a participant asking the assistant to change mode, and
 * which change they asked for.
 *
 * The message must address LMA by name — the explicit addressee is what keeps
 * ordinary meeting chat out of the matcher, exactly as it does for the dismissal
 * commands, and it is why a bare "translate" is somebody talking to the meeting
 * rather than to the assistant — and the rest of it, once the addressee is
 * removed, must be one of the phrases above and nothing else.
 *
 * There is no length guard here, unlike `matchesEndCommand`: requiring the whole
 * message to be the addressee plus one known phrase already excludes everything
 * a length check would, so a guard would be unreachable rather than protective.
 *
 * Anyone in the meeting may issue these, like the existing `LMA end`. The
 * assistant's mode is a property of the meeting everybody is in, so the person
 * who needs a translator is as likely to be a guest as the host.
 */
export function parseAssistantChatCommand(message: string): AssistantChatCommand | null {
    if (!message) return null;

    const normalized = normalize(message);
    if (!normalized) return null;

    const tokens = normalized.split(' ');
    const addressee = tokens.indexOf('lma');
    if (addressee === -1) return null;

    const remainder = [...tokens.slice(0, addressee), ...tokens.slice(addressee + 1)].join(' ');
    const command = CHAT_COMMAND_INDEX.get(remainder);
    if (!command) return null;

    return { ...command };
}

// ---------------------------------------------------------------------------
// Applying a chat command
// ---------------------------------------------------------------------------

/** How a mode is described to participants in chat and on the meeting page. */
export function describeMeetingMode(mode: MeetingMode): string {
    switch (mode) {
        case 'translator':
            return 'translator mode';
        case 'group':
            return 'group mode';
        case 'normal':
            return 'assistant mode';
        default: {
            const unclassified: never = mode;
            return unclassified;
        }
    }
}

/** How an activation mode is described to participants. */
export function describeActivationMode(mode: ActivationMode): string {
    switch (mode) {
        case 'always_active':
            return 'always listening';
        case 'wake_phrase':
            return 'listening for the wake phrase';
        case 'strands_tool':
            return 'listening when the meeting assistant asks it to';
        default: {
            const unclassified: never = mode;
            return unclassified;
        }
    }
}

/**
 * The chat reply for an outcome: one line stating what the assistant is now
 * doing, or why it is not doing anything different.
 *
 * A participant who typed a command gets an answer either way. A command that
 * changed nothing is the one most likely to be retyped, so it says so rather
 * than staying silent and looking broken.
 */
export function assistantToggleChatReply(outcome: AssistantReconfigureOutcome): string[] {
    const prefix = 'Live Meeting Assistant:';

    if (outcome.result !== 'applied') {
        const detail = outcome.detail || 'no change made';
        return [`${prefix} ${detail}${/[.!?]$/.test(detail) ? '' : '.'}`];
    }

    const parts: string[] = [];
    if (outcome.changed.includes('meetingMode')) {
        parts.push(`switched to ${describeMeetingMode(outcome.meetingModeAfter)}`);
    }
    if (outcome.changed.includes('activationMode')) {
        parts.push(`now ${describeActivationMode(outcome.activationModeAfter)}`);
    }
    return [`${prefix} ${parts.join(', ')}.`];
}

/**
 * Hand a parsed chat command to the voice assistant and produce the chat reply.
 *
 * Takes the provider rather than reaching for the singleton so the chat path can
 * be exercised without a Nova session, and so the meeting-page and voice routes
 * can reuse it.
 */
export async function applyAssistantChatCommand(
    assistant: Pick<VoiceAssistantProvider, 'isEnabled' | 'reconfigure'>,
    command: AssistantChatCommand,
    requestedBy?: string | null,
): Promise<string[]> {
    if (!assistant.isEnabled()) {
        return ['Live Meeting Assistant: the voice assistant is not enabled for this meeting.'];
    }

    const outcome = await assistant.reconfigure({
        meetingMode: command.meetingMode,
        activationMode: command.activationMode,
        route: 'chat',
        requestedBy,
    });

    return assistantToggleChatReply(outcome);
}

// ---------------------------------------------------------------------------
// The log line
// ---------------------------------------------------------------------------

/** The tag every toggle log line carries, so one grep finds all of them. */
export const ASSISTANT_TOGGLE_LOG_TAG = '[assistant-toggle]';

export interface AssistantToggleIdentity {
    vpId: string;
    callId: string;
}

/** The Virtual Participant and call this process is running, for the log line. */
export function assistantToggleIdentity(): AssistantToggleIdentity {
    return {
        vpId: details.invite.virtualParticipantId || '-',
        callId: process.env.VP_CALL_ID || '-',
    };
}

/**
 * Reduce a meeting-supplied value to something that reads as one field of one
 * log record.
 *
 * Sender names come from the meeting platform's chat, so what survives is
 * letters, digits and the punctuation names actually contain, on a single line
 * and bounded in length. That keeps one toggle to one line, and keeps a name
 * from reading as another `key=value` field to anyone grepping the log. Same
 * spirit as the treatment `exitMessagesFor` gives a requester name.
 */
function logSafe(value: string): string {
    return value
        .replace(/[^A-Za-z0-9 ._@'\-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 80);
}

function axis(name: string, before: string, after: string): string {
    return before === after ? `${name}=${after}(unchanged)` : `${name}=${before}->${after}`;
}

/**
 * The single line written for every attempted toggle, applied or not.
 *
 * It names what changed, which route asked, who asked, which Virtual Participant
 * it was, and whether the model session was rebuilt — everything needed to read
 * a live test back out of CloudWatch without correlating several lines.
 */
export function formatAssistantToggleLog(
    request: AssistantReconfigureRequest,
    outcome: AssistantReconfigureOutcome,
    identity: AssistantToggleIdentity = assistantToggleIdentity(),
): string {
    const fields = [
        ASSISTANT_TOGGLE_LOG_TAG,
        `vp=${logSafe(identity.vpId)}`,
        `call=${logSafe(identity.callId)}`,
        `route=${request.route}`,
        `by="${request.requestedBy ? logSafe(request.requestedBy) : '-'}"`,
        `result=${outcome.result}`,
        axis('meetingMode', outcome.meetingModeBefore, outcome.meetingModeAfter),
        axis('activationMode', outcome.activationModeBefore, outcome.activationModeAfter),
        `session=${outcome.sessionRebuilt ? 'rebuilt' : 'kept'}`,
    ];
    if (outcome.detail) {
        fields.push(`detail="${logSafe(outcome.detail)}"`);
    }
    return fields.join(' ');
}
