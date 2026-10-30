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
 * (`normal` assistant / `group` listening — `listen` in the chat — /
 * `translator`), which came from the Nova Sonic configuration record and was
 * read once at task start, and the
 * activation mode (`always_active` / `wake_phrase`), which came from a stack
 * parameter. Changing either meant editing configuration and starting a new
 * meeting.
 *
 * This module holds the parts that every route into that change can share: the
 * request and outcome shapes, the chat vocabulary, the confirmation wording, and
 * the single log line each toggle emits. The meeting chat is the only route that
 * calls it today; a route differs only in how it arrives at a request, so
 * another one supplies an `AssistantToggleRoute` and reuses everything below.
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
// So "@LMA, translate!" and "lma translate" arrive at the matcher the same.
import { normalizeChatText as normalize } from './chat-text.js';
import {
    foldLanguageText,
    meetingLanguage,
    resolveTranslatorLanguage,
    supportedTranslatorLanguageNames,
} from './translator-languages.js';

/**
 * Which surface asked for the change, recorded on every toggle so one log line
 * says where a change came from.
 *
 * Only `chat` has a caller today — the other three are the surfaces the log
 * would otherwise be unable to distinguish once they do, and naming them here
 * keeps that a one-line change rather than a change to the outcome shape and
 * every provider.
 */
export type AssistantToggleRoute = 'chat' | 'voice' | 'api' | 'config';

export interface AssistantReconfigureRequest {
    /** Meeting mode to switch to. Omitted leaves the current mode alone. */
    meetingMode?: MeetingMode;
    /** Activation mode to switch to. Omitted leaves the current mode alone. */
    activationMode?: ActivationMode;
    /**
     * The translator's language pair for the rest of this meeting. `b` is the
     * language asked for; `a` is the meeting's own language, omitted when the
     * route could not tell, in which case the provider keeps its configured
     * first language. Omitted leaves the pair alone. Never persisted.
     */
    translatorLanguages?: { a?: string; b: string };
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
    /** The fields that changed. Empty unless `result` is `applied`. */
    changed: Array<'meetingMode' | 'activationMode' | 'translatorLanguages'>;
    /** Whether the provider tore down and rebuilt its model session. */
    sessionRebuilt: boolean;
    meetingModeBefore: MeetingMode;
    meetingModeAfter: MeetingMode;
    activationModeBefore: ActivationMode;
    activationModeAfter: ActivationMode;
    /** The translator's language pair once the request settled, set when the
     *  assistant ends up in translator mode. */
    translatorLanguagesAfter?: { a: string; b: string };
    /**
     * Extra context for the participant and for the log. Always set when
     * `result` is not `applied`, saying why; may also be set on an applied
     * request that has not finished settling, such as one whose session rebuild
     * is still to come.
     */
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
 * Whether a meeting mode only works while the assistant is always listening.
 *
 * `group` starts muted and `activate()` deliberately does not unmute it, because
 * group mode owns its own muted-until-addressed state machine — and that machine
 * runs on the model's USER transcripts, which only arrive while a session is
 * open. `translator` speaks on every completed utterance, and receives none
 * between activations. On `wake_phrase` activation no session is held between
 * activations, so either mode would be silent rather than merely limited.
 *
 * Stated once, and applied in both directions: the chat commands for these two
 * modes ask for `always_active` alongside the mode, and `reconfigure` declines a
 * request whose resulting combination would be one of these modes without it.
 */
export function requiresAlwaysListening(mode: MeetingMode): boolean {
    return mode === 'group' || mode === 'translator';
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
    /** `LMA translate <language>`: the supported language asked for, by its
     *  canonical name. */
    translatorLanguage?: string;
    /** `LMA translate <word>` where the word is not a supported language. The
     *  command carries no change; it is answered with the supported list. */
    unsupportedLanguage?: string;
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
 * `translate` and `listen` (`group`) both also set `always_active`, because neither mode
 * works without it. A translator speaks on every completed utterance, so on
 * `wake_phrase` activation it receives no audio until somebody says the wake
 * phrase and translates nothing in between. Group mode is worse: it starts muted
 * and `activate()` deliberately does not unmute it, because group mode owns its
 * own muted-until-addressed state machine — and that machine runs on Nova's USER
 * transcripts, which only arrive while a session is open. On `wake_phrase` there
 * is no session between activations, so a group-mode assistant would never
 * become audible at all. Asking for a mode and getting permanent silence is not
 * a reading of these commands worth supporting.
 *
 * `assist` carries no activation change of its own: `normal` mode works under
 * either activation mode. When `translate` or `listen` switched activation on
 * the way in, though, leaving that mode puts back the activation mode it
 * replaced — see `createActivationRestore`, which `applyAssistantChatCommand`
 * consults, so a wake-phrase meeting that was briefly translated goes back to
 * the wake phrase rather than staying always listening.
 *
 * Grouped by the change each set of phrases asks for, with the first phrase of
 * each group as its canonical form, so the `LMA help` reply (chat-help.ts) is
 * generated from this table rather than kept beside it.
 */
export interface AssistantChatCommandEntry {
    /** Every phrase that asks for this change. The first is the canonical
     *  form — the one the help message lists. */
    readonly phrases: readonly string[];
    readonly command: AssistantChatCommand;
    /** What the command does, as the help message describes it. */
    readonly summary: string;
}

export const ASSISTANT_CHAT_COMMANDS: ReadonlyArray<AssistantChatCommandEntry> = [
    // Meeting mode — what job the assistant is doing.
    {
        phrases: ['translate', 'translator', 'translate mode', 'translator mode', 'interpret'],
        command: { meetingMode: 'translator', activationMode: 'always_active' },
        summary: 'translator mode; add a language to pick it',
    },
    {
        phrases: ['assist', 'assistant', 'assist mode', 'assistant mode', 'normal', 'normal mode'],
        command: { meetingMode: 'normal' },
        summary: 'assistant mode',
    },
    {
        // `listen` is the name participants are told and the one `LMA help`
        // lists; `group` / `group mode` are accepted as well because `group`
        // is the configuration value's name.
        phrases: ['listen', 'listen mode', 'listening mode', 'group', 'group mode'],
        command: { meetingMode: 'group', activationMode: 'always_active' },
        summary: 'listen mode, quiet until addressed',
    },
    // Activation mode — when the assistant is listening.
    {
        phrases: ['always', 'always on', 'always active', 'always listen', 'open mic'],
        command: { activationMode: 'always_active' },
        summary: 'always listening',
    },
    {
        phrases: ['wake', 'wake word', 'wake phrase', 'on request'],
        command: { activationMode: 'wake_phrase' },
        summary: 'wake phrase only',
    },
];

const CHAT_COMMANDS: ReadonlyArray<readonly [string, AssistantChatCommand]> = ASSISTANT_CHAT_COMMANDS.flatMap(
    (entry) => entry.phrases.map((phrase) => [phrase, entry.command] as const),
);

const CHAT_COMMAND_INDEX: ReadonlyMap<string, AssistantChatCommand> = new Map(CHAT_COMMANDS);

/**
 * `translate <language>` and its variants: the translate phrases, optionally
 * followed by "to" / "into" / "in", then the language. The language part is
 * matched against the closed list in translator-languages.ts.
 */
const TRANSLATE_WITH_LANGUAGE = /^(?:translate|translator|interpret)(?: mode)?(?: (?:to|into|in))? (.+)$/;

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

    // Accents are folded as well, so "LMA translate Español" keeps its
    // language rather than splitting it at the accent.
    const normalized = foldLanguageText(message);
    if (!normalized) return null;

    const tokens = normalized.split(' ');
    const addressee = tokens.indexOf('lma');
    if (addressee === -1) return null;

    const remainder = [...tokens.slice(0, addressee), ...tokens.slice(addressee + 1)].join(' ');
    const command = CHAT_COMMAND_INDEX.get(remainder);
    if (command) return { ...command };

    // `translate <language>`. A single unknown word of letters is answered
    // with the supported list, because "LMA translate klingon" is plainly a
    // request;
    // several unknown words ("LMA translate the slides") are ordinary chat and
    // are left alone, like any other message that is not a command.
    const withLanguage = TRANSLATE_WITH_LANGUAGE.exec(remainder);
    if (!withLanguage) return null;
    const language = resolveTranslatorLanguage(withLanguage[1]);
    if (language) {
        return { ...CHAT_COMMAND_INDEX.get('translate'), translatorLanguage: language.name };
    }
    if (/^[a-z]{2,20}$/.test(withLanguage[1])) {
        return { unsupportedLanguage: withLanguage[1] };
    }
    return null;
}

/**
 * How long the same chat message is treated as a repeat of itself rather than a
 * fresh command. Long enough to cover a DOM observer emitting one message twice,
 * short enough that a participant retyping a command because they expected
 * something to happen still gets an answer.
 */
const DUPLICATE_WINDOW_MS = 5000;

/**
 * Answer the same toggle message once, for a platform that can deliver it twice.
 *
 * The Webex chat observer emits duplicate add events, which the end command
 * handles with a latch of its own. A duplicate toggle does not rebuild the
 * session twice — the no-op guard in `reconfigure` sees the assistant already in
 * the requested mode — but it does produce a second chat reply, so one command
 * reads as "switched to translator mode." immediately followed by "already in
 * the requested configuration.". This drops the repeat instead.
 *
 * Deliberately a short window rather than a permanent latch: typing the same
 * command again a minute later is a real request, not a duplicate event. And
 * keyed on the sender as well as the text, because two participants typing the
 * same command seconds apart are two requests — each of which is owed an answer
 * — not one event delivered twice.
 */
export function createDuplicateToggleFilter(windowMs: number = DUPLICATE_WINDOW_MS) {
    let lastKey: string | null = null;
    let lastAt = 0;
    return function shouldHandle(
        message: string,
        sender?: string | null,
        now: number = Date.now(),
    ): boolean {
        const key = `${normalize(sender || '')}|${normalize(message)}`;
        if (key === lastKey && now - lastAt < windowMs) {
            return false;
        }
        lastKey = key;
        lastAt = now;
        return true;
    };
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
            return 'listen mode';
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
    const pair = outcome.translatorLanguagesAfter;
    if (outcome.changed.includes('meetingMode')) {
        const pairText = pair ? ` (${pair.a} and ${pair.b})` : '';
        parts.push(`switched to ${describeMeetingMode(outcome.meetingModeAfter)}${pairText}`);
    } else if (outcome.changed.includes('translatorLanguages') && pair) {
        parts.push(`now translating between ${pair.a} and ${pair.b}`);
    }
    if (outcome.changed.includes('activationMode')) {
        parts.push(`now ${describeActivationMode(outcome.activationModeAfter)}`);
    }
    // A change that landed but has not finished settling says so, rather than
    // reading as complete while the assistant is still catching up.
    if (outcome.detail) {
        parts.push(outcome.detail);
    }
    return [`${prefix} ${parts.join(', ')}.`];
}

/**
 * Remembers the activation mode a forced-always-listening meeting mode replaced,
 * so leaving that mode can put it back.
 *
 * `translate` and `listen` ask for `always_active` because neither mode works
 * without it (see `requiresAlwaysListening`). That is a side effect of the mode,
 * not something the participant chose, so when the meeting later leaves the
 * mode — `LMA assist`, today the only mode that does not need always-listening —
 * the activation mode in effect before it was forced comes back with it. Without
 * this a meeting deployed on `wake_phrase` that was translated for five minutes
 * stays always listening, and streaming audio to the model, for the rest of the
 * meeting.
 *
 * The memory is cleared by an explicit activation command (`LMA always`,
 * `LMA wake`) that the assistant accepted or was already in: the participant
 * has then said what they want, and that wins over what the meeting started
 * with. A declined `LMA wake` is recorded instead: it is declined while a
 * translator or group-mode assistant is running, and the participant who typed
 * it still wants the wake phrase once that mode ends — including in a meeting
 * that was always listening before translation started, where there would
 * otherwise be nothing to restore.
 *
 * Only the first forced switch is remembered. Going from translator straight to
 * group changes no activation (it is already `always_active`), so the original
 * mode is still the one restored when the meeting finally returns to `assist`.
 *
 * One instance per process is enough: a Virtual Participant process runs one
 * meeting. The factory exists so tests can start from an empty memory.
 */
export function createActivationRestore() {
    let restoreTo: ActivationMode | null = null;

    return {
        /** The activation mode a later `assist` would restore, if any. */
        pending(): ActivationMode | null {
            return restoreTo;
        },

        /**
         * The request to send for `command`: the command itself, plus the
         * remembered activation mode when the command leaves a forcing mode and
         * names no activation of its own.
         */
        requestFor(command: AssistantChatCommand): AssistantChatCommand {
            if (
                restoreTo !== null
                && command.meetingMode !== undefined
                && !requiresAlwaysListening(command.meetingMode)
                && command.activationMode === undefined
            ) {
                return { ...command, activationMode: restoreTo };
            }
            return { ...command };
        },

        /** Update the memory from what was asked for and how it ended. */
        record(command: AssistantChatCommand, sent: AssistantChatCommand, outcome: AssistantReconfigureOutcome): void {
            if (outcome.result === 'declined') {
                // An activation request refused while a forcing mode is running
                // is a wish for when the mode ends, not a request that did not
                // happen. That holds for any refused `LMA wake`, whether the
                // combination rule or a switch still in progress refused it —
                // either way the participant asked to stop listening
                // continuously. A refused `LMA always` cannot be the
                // combination rule, so it is not taken as a wish.
                if (
                    command.meetingMode === undefined
                    && command.activationMode !== undefined
                    && command.activationMode !== 'always_active'
                    && requiresAlwaysListening(outcome.meetingModeBefore)
                ) {
                    restoreTo = command.activationMode;
                }
                return;
            }

            // An explicit activation command is the participant's own choice.
            if (command.meetingMode === undefined && command.activationMode !== undefined) {
                restoreTo = null;
                return;
            }

            // The restore was sent and either landed or was already true.
            if (sent.activationMode !== undefined && command.activationMode === undefined) {
                restoreTo = null;
                return;
            }

            // Entering a forcing mode that actually moved activation: remember
            // where it moved it from, unless an earlier forced switch already did.
            if (
                command.meetingMode !== undefined
                && requiresAlwaysListening(command.meetingMode)
                && outcome.changed.includes('activationMode')
                && restoreTo === null
            ) {
                restoreTo = outcome.activationModeBefore;
            }
        },
    };
}

export type ActivationRestore = ReturnType<typeof createActivationRestore>;

/** The memory for the meeting this process is running. */
const meetingActivationRestore: ActivationRestore = createActivationRestore();

/**
 * Hand a parsed chat command to the voice assistant and produce the chat reply.
 *
 * Takes the provider rather than reaching for the singleton so the chat path can
 * be exercised without a Nova session, and so another route can reuse it.
 *
 * Every provider is asked, including the one standing in for a meeting with no
 * voice assistant: each declines with its own reason, and asking them keeps one
 * attempt to one `[assistant-toggle]` line whatever the deployment is running.
 *
 * The caller awaits the whole change, rebuild included. On the platforms whose
 * chat arrives on a poll loop that is worth knowing: the loop handles messages
 * one at a time, so a message sent immediately after a mode command waits for
 * the rebuild. Reckon on about a second — the teardown alone holds half of that
 * in fixed delays — and on several if it has to wait behind a session refresh
 * already in flight, which the provider bounds at five seconds.
 */
export async function applyAssistantChatCommand(
    assistant: Pick<VoiceAssistantProvider, 'reconfigure'>,
    command: AssistantChatCommand,
    requestedBy?: string | null,
    restore: ActivationRestore = meetingActivationRestore,
): Promise<string[]> {
    const prefix = 'Live Meeting Assistant:';
    if (command.unsupportedLanguage !== undefined) {
        console.log(
            `${ASSISTANT_TOGGLE_LOG_TAG} route=chat result=declined`
                + ` detail="unsupported translator language ${logSafe(command.unsupportedLanguage)}"`,
        );
        return [
            `${prefix} I can't translate into "${command.unsupportedLanguage}". `
                + `Supported languages: ${supportedTranslatorLanguageNames().join(', ')}.`,
        ];
    }

    // The other half of the pair is the meeting's own language, from how it is
    // being transcribed. When that is unknown the provider keeps its configured
    // first language.
    let translatorLanguages: AssistantReconfigureRequest['translatorLanguages'];
    if (command.translatorLanguage !== undefined) {
        const own = meetingLanguage(details.transcribeLanguageCode, details.transcribePreferredLanguage);
        if (own && own.name === command.translatorLanguage) {
            return [
                `${prefix} ${own.name} is the language this meeting is transcribed in.`
                    + ' Name the language to translate it into, for example "LMA translate French".',
            ];
        }
        translatorLanguages = { a: own?.name, b: command.translatorLanguage };
    }

    const sent = restore.requestFor(command);
    if (sent.activationMode !== command.activationMode) {
        console.log(
            `${ASSISTANT_TOGGLE_LOG_TAG} requesting restore of activationMode=${sent.activationMode}`
                + ` on leaving an always-listening meeting mode for meetingMode=${command.meetingMode}`,
        );
    }
    const outcome = await assistant.reconfigure({
        meetingMode: sent.meetingMode,
        activationMode: sent.activationMode,
        ...(translatorLanguages ? { translatorLanguages } : {}),
        route: 'chat',
        requestedBy,
    });
    restore.record(command, sent, outcome);

    const reply = assistantToggleChatReply(outcome);
    // A language set from chat lasts for this meeting and is not written to
    // the configuration store. A chat sender is known only by the display name
    // the meeting platform shows, which the participant chooses, so no sender
    // can be tied to the LMA user who launched this Virtual Participant; the
    // deployment default stays an Admin setting on the configuration page.
    if (translatorLanguages && outcome.result === 'applied') {
        return [
            `${reply[0]} This applies to this meeting only; the default language pair is set`
                + ' on the Nova Sonic Configuration page.',
        ];
    }
    return reply;
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
    if (request.translatorLanguages) {
        const pair = outcome.translatorLanguagesAfter;
        const unchanged = outcome.changed.includes('translatorLanguages') ? '' : '(unchanged)';
        fields.push(`translator="${pair ? `${logSafe(pair.a)}<->${logSafe(pair.b)}` : '-'}"${unchanged}`);
    }
    if (outcome.detail) {
        fields.push(`detail="${logSafe(outcome.detail)}"`);
    }
    return fields.join(' ');
}
