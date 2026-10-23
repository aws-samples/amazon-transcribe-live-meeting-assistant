/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The chat vocabulary for changing the assistant's mode mid-meeting.
 *
 * Two invariants matter equally, the same pair `stop-request.test.ts` holds for
 * the transcript route. A participant who types a command has to be understood
 * however they phrase it, and ordinary meeting chat that happens to mention LMA
 * and a mode word has to leave the assistant alone — a false positive here
 * switches a live meeting into translator mode, which is both disruptive and
 * confusing to undo.
 *
 * The existing dismissal vocabulary has to keep meaning dismissal, so the two
 * vocabularies are asserted to be disjoint rather than left to chance.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    ASSISTANT_TOGGLE_LOG_TAG,
    applyAssistantChatCommand,
    assistantToggleChatReply,
    describeActivationMode,
    describeMeetingMode,
    formatAssistantToggleLog,
    isActivationMode,
    isMeetingMode,
    parseAssistantChatCommand,
    type AssistantReconfigureOutcome,
    type AssistantReconfigureRequest,
} from './assistant-toggles.js';
import { createDuplicateToggleFilter } from './assistant-toggles.js';
import { details, matchesEndCommand } from './details.js';

// ---------------------------------------------------------------------------
// The vocabulary
// ---------------------------------------------------------------------------

test('a mode command addressed to LMA is recognised however it is punctuated', () => {
    const translator = [
        'LMA translate',
        'LMA, translate!',
        '@LMA translate',
        'lma translate',
        'LMA TRANSLATE',
        'translate LMA',
        'LMA translator',
        'LMA translator mode',
        'LMA translate mode',
        'LMA interpret',
    ];
    for (const message of translator) {
        const command = parseAssistantChatCommand(message);
        assert.notEqual(command, null, `not recognised: "${message}"`);
        assert.equal(command?.meetingMode, 'translator', `wrong mode for "${message}"`);
    }

    const assistant = ['LMA assist', 'LMA assistant', 'LMA assistant mode', 'LMA normal', 'LMA normal mode'];
    for (const message of assistant) {
        assert.equal(parseAssistantChatCommand(message)?.meetingMode, 'normal', `wrong mode for "${message}"`);
    }

    for (const message of ['LMA group', 'LMA group mode']) {
        assert.equal(parseAssistantChatCommand(message)?.meetingMode, 'group', `wrong mode for "${message}"`);
    }
});

test('an activation command addressed to LMA is recognised', () => {
    const always = ['LMA always', 'LMA always on', 'LMA always active', 'LMA always listen', 'LMA open mic'];
    for (const message of always) {
        const command = parseAssistantChatCommand(message);
        assert.equal(command?.activationMode, 'always_active', `wrong activation for "${message}"`);
        assert.equal(command?.meetingMode, undefined, `"${message}" must not also change the meeting mode`);
    }

    const wake = ['LMA wake', 'LMA wake word', 'LMA wake phrase', 'LMA on request'];
    for (const message of wake) {
        const command = parseAssistantChatCommand(message);
        assert.equal(command?.activationMode, 'wake_phrase', `wrong activation for "${message}"`);
        assert.equal(command?.meetingMode, undefined, `"${message}" must not also change the meeting mode`);
    }
});

test('the two modes that need an always-listening assistant ask for one', () => {
    // Neither works without it, and a deployment on wake_phrase activation is
    // where both would otherwise be asked for.
    //
    // A translator speaks on every completed utterance, so on wake_phrase it
    // receives no audio until somebody says the wake phrase and translates
    // nothing in between. Group mode is worse: it starts muted, activate()
    // deliberately does not unmute it, and the state machine that would is
    // driven by Nova USER transcripts, which only arrive while a session is
    // open — on wake_phrase there is none between activations, so a group-mode
    // assistant would never become audible at all.
    assert.deepEqual(parseAssistantChatCommand('LMA translate'), {
        meetingMode: 'translator',
        activationMode: 'always_active',
    });
    assert.deepEqual(parseAssistantChatCommand('LMA group'), {
        meetingMode: 'group',
        activationMode: 'always_active',
    });
    for (const message of ['LMA translator mode', 'LMA interpret', 'LMA group mode']) {
        assert.equal(
            parseAssistantChatCommand(message)?.activationMode,
            'always_active',
            `"${message}" must also ask for an always-listening assistant`,
        );
    }

    // `normal` works under either activation mode, and the command has no record
    // of what activation was before translator or group changed it.
    assert.equal(parseAssistantChatCommand('LMA assist')?.activationMode, undefined);
    assert.equal(parseAssistantChatCommand('LMA normal')?.activationMode, undefined);
});

test('ordinary meeting chat is left alone', () => {
    // Each of these would switch a live meeting into a different mode if the
    // match were keyword-based rather than whole-message.
    const prose = [
        'the meeting will end at 3pm',
        'can LMA translate the slides?',
        'LMA can you translate this for me later',
        'we should always use LMA for these',
        'LMA is always on in my meetings',
        'I think LMA translator mode is great',
        "I'll wake LMA later",
        'LMA group chat',
        'the LMA group meets on Tuesdays',
        'does LMA have a normal mode and a group mode',
        'translate the agenda please',
        'always on',
        'wake word',
        'LMA',
        '@LMA',
        '',
        '   ',
    ];
    for (const message of prose) {
        assert.equal(parseAssistantChatCommand(message), null, `wrongly recognised: "${message}"`);
    }
});

test('a command word nobody addressed to the assistant is somebody talking to the meeting', () => {
    // The whole point of the addressee. "translate" on its own, in a meeting
    // with speakers of two languages, is one participant asking another.
    for (const message of ['translate', 'translator', 'interpret', 'assist', 'group', 'always', 'wake', 'normal']) {
        assert.equal(parseAssistantChatCommand(message), null, `wrongly recognised: "${message}"`);
    }
});

test('a command phrase inside a longer message is not a command', () => {
    // Whole-message matching is what holds this, so it keeps holding however
    // long the message is.
    const padded = `LMA translate ${'x'.repeat(60)}`;
    assert.equal(parseAssistantChatCommand(padded), null);
    assert.equal(parseAssistantChatCommand(`${'x'.repeat(60)} LMA translate`), null);
});

test('the dismissal vocabulary and the toggle vocabulary do not overlap', () => {
    // "LMA stop" has meant "leave the meeting" since the VP shipped. A toggle
    // that also claimed it would either strand the dismissal or silently change
    // what it does, depending on which branch each platform checks first.
    const dismissals = [
        'LMA end',
        'LMA leave',
        'LMA stop',
        'LMA quit',
        'LMA exit',
        'LMA goodbye',
        'LMA bye',
        'Goodbye LMA',
    ];
    for (const message of dismissals) {
        assert.equal(matchesEndCommand(message), true, `no longer a dismissal: "${message}"`);
        assert.equal(parseAssistantChatCommand(message), null, `dismissal claimed as a toggle: "${message}"`);
    }

    const toggles = ['LMA translate', 'LMA assist', 'LMA group', 'LMA always', 'LMA wake'];
    for (const message of toggles) {
        assert.equal(matchesEndCommand(message), false, `toggle claimed as a dismissal: "${message}"`);
    }
});

test('the toggle vocabulary does not collide with the pause and resume commands', () => {
    // Most platforms match pause/resume with a case-sensitive substring test on
    // the raw message, so a toggle phrase containing either word would both
    // toggle and pause depending on branch order. Asserted against the
    // configured commands rather than the literals, so the two stay aligned if
    // the commands change.
    for (const message of ['LMA translate', 'LMA assist', 'LMA group', 'LMA always', 'LMA wake', 'LMA open mic']) {
        assert.equal(
            message.includes(details.pauseCommand),
            false,
            `"${message}" would also match the pause command "${details.pauseCommand}"`,
        );
        assert.equal(
            message.includes(details.startCommand),
            false,
            `"${message}" would also match the resume command "${details.startCommand}"`,
        );
    }
});

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

test('only the configured modes are accepted', () => {
    for (const mode of ['normal', 'group', 'translator']) {
        assert.equal(isMeetingMode(mode), true);
    }
    for (const mode of ['Translator', 'TRANSLATOR', 'interpreter', '', 'normal ', undefined, null, 7, {}]) {
        assert.equal(isMeetingMode(mode), false, `accepted ${JSON.stringify(mode)}`);
    }

    for (const mode of ['always_active', 'wake_phrase', 'strands_tool']) {
        assert.equal(isActivationMode(mode), true);
    }
    for (const mode of ['always', 'wake', 'alwaysActive', '', undefined, null, 7, {}]) {
        assert.equal(isActivationMode(mode), false, `accepted ${JSON.stringify(mode)}`);
    }
});

// ---------------------------------------------------------------------------
// What the meeting is told
// ---------------------------------------------------------------------------

test('every mode has wording for participants', () => {
    assert.equal(describeMeetingMode('normal'), 'assistant mode');
    assert.equal(describeMeetingMode('group'), 'group mode');
    assert.equal(describeMeetingMode('translator'), 'translator mode');
    assert.equal(describeActivationMode('always_active'), 'always listening');
    assert.equal(describeActivationMode('wake_phrase'), 'listening for the wake phrase');
    assert.equal(describeActivationMode('strands_tool'), 'listening when the meeting assistant asks it to');
});

function outcome(over: Partial<AssistantReconfigureOutcome> = {}): AssistantReconfigureOutcome {
    return {
        result: 'applied',
        changed: ['meetingMode'],
        sessionRebuilt: true,
        meetingModeBefore: 'normal',
        meetingModeAfter: 'translator',
        activationModeBefore: 'always_active',
        activationModeAfter: 'always_active',
        ...over,
    };
}

test('a change that landed is confirmed in chat, naming only what changed', () => {
    assert.deepEqual(assistantToggleChatReply(outcome()), [
        'Live Meeting Assistant: switched to translator mode.',
    ]);

    assert.deepEqual(
        assistantToggleChatReply(
            outcome({
                changed: ['meetingMode', 'activationMode'],
                activationModeBefore: 'wake_phrase',
                activationModeAfter: 'always_active',
            }),
        ),
        ['Live Meeting Assistant: switched to translator mode, now always listening.'],
    );

    assert.deepEqual(
        assistantToggleChatReply(
            outcome({
                changed: ['activationMode'],
                meetingModeAfter: 'normal',
                activationModeBefore: 'always_active',
                activationModeAfter: 'wake_phrase',
            }),
        ),
        ['Live Meeting Assistant: now listening for the wake phrase.'],
    );
});

test('a command that changed nothing says so rather than staying silent', () => {
    // The command most likely to be retyped is the one that appeared to do
    // nothing, so it gets an answer too.
    assert.deepEqual(
        assistantToggleChatReply(
            outcome({
                result: 'unchanged',
                changed: [],
                sessionRebuilt: false,
                meetingModeAfter: 'normal',
                detail: 'already in the requested configuration',
            }),
        ),
        ['Live Meeting Assistant: already in the requested configuration.'],
    );
});

// ---------------------------------------------------------------------------
// Handing a command to a provider
// ---------------------------------------------------------------------------

/** A provider that records what it was asked for, with no session behind it. */
function fakeAssistant(enabled: boolean, result: Partial<AssistantReconfigureOutcome> = {}) {
    const requests: AssistantReconfigureRequest[] = [];
    return {
        requests,
        isEnabled: () => enabled,
        reconfigure: async (request: AssistantReconfigureRequest) => {
            requests.push(request);
            return outcome(result);
        },
    };
}

test('a chat command reaches the provider tagged with the chat route and the sender', () => {
    // The route and the requester are what make the log line diagnostic, and
    // chat is the only route that can supply a name today.
    const assistant = fakeAssistant(true);
    return applyAssistantChatCommand(assistant, parseAssistantChatCommand('LMA translate')!, 'Alice').then((reply) => {
        assert.deepEqual(assistant.requests, [
            {
                meetingMode: 'translator',
                activationMode: 'always_active',
                route: 'chat',
                requestedBy: 'Alice',
            },
        ]);
        assert.deepEqual(reply, ['Live Meeting Assistant: switched to translator mode.']);
    });
});

test('a chat message delivered twice in quick succession is answered once', () => {
    // The Webex chat observer emits duplicate add events. The session is not
    // rebuilt twice either way, but the duplicate would post a second reply
    // right after the first — "switched to translator mode." followed by
    // "already in the requested configuration.".
    const fresh = createDuplicateToggleFilter(5000);
    assert.equal(fresh('LMA translate', 1_000), true);
    assert.equal(fresh('LMA translate', 1_040), false, 'the duplicate event must be dropped');
    assert.equal(fresh('@LMA, Translate!', 1_080), false, 'the same command differently typed is still the same');

    // A different command in the window is not a duplicate of the first.
    assert.equal(fresh('LMA wake', 1_120), true);

    // And the same command typed again later is a real request.
    assert.equal(fresh('LMA wake', 20_000), true);
});

test('a meeting with no voice assistant is told so and the provider is not called', async () => {
    const assistant = fakeAssistant(false);
    const reply = await applyAssistantChatCommand(assistant, parseAssistantChatCommand('LMA translate')!, 'Alice');
    assert.deepEqual(reply, ['Live Meeting Assistant: the voice assistant is not enabled for this meeting.']);
    assert.deepEqual(assistant.requests, []);
});

// ---------------------------------------------------------------------------
// The log line
// ---------------------------------------------------------------------------

const IDENTITY = { vpId: 'vp-123', callId: 'call-456' };

test('one line records what changed, which route asked, who asked and which VP', () => {
    const line = formatAssistantToggleLog(
        { meetingMode: 'translator', route: 'chat', requestedBy: 'Alice' },
        outcome({ changed: ['meetingMode', 'activationMode'], activationModeBefore: 'wake_phrase' }),
        IDENTITY,
    );
    assert.equal(
        line,
        `${ASSISTANT_TOGGLE_LOG_TAG} vp=vp-123 call=call-456 route=chat by="Alice" result=applied ` +
            'meetingMode=normal->translator activationMode=wake_phrase->always_active session=rebuilt',
    );
});

test('an axis nobody asked about is marked unchanged rather than omitted', () => {
    // Both axes appear on every line so a grep can read the whole configuration
    // off any one of them, without having to find the line that last changed
    // the other.
    const line = formatAssistantToggleLog({ meetingMode: 'translator', route: 'api' }, outcome(), IDENTITY);
    assert.match(line, /meetingMode=normal->translator/);
    assert.match(line, /activationMode=always_active\(unchanged\)/);
    assert.match(line, /by="-"/);
});

test('a request that changed nothing and one that was refused are recorded differently', () => {
    // "Already doing that" and "will not do that" are both "nothing happened"
    // and read nothing alike in a log, so they are separate results rather than
    // one inferred from whether anything changed.
    const unchanged = formatAssistantToggleLog(
        { meetingMode: 'normal', route: 'chat' },
        outcome({
            result: 'unchanged',
            changed: [],
            sessionRebuilt: false,
            meetingModeAfter: 'normal',
            detail: 'already in the requested configuration',
        }),
        IDENTITY,
    );
    assert.match(unchanged, /result=unchanged/);
    assert.match(unchanged, /session=kept/);
    assert.match(unchanged, /detail="already in the requested configuration"/);

    const declined = formatAssistantToggleLog(
        { meetingMode: 'translator', route: 'chat' },
        outcome({
            result: 'declined',
            changed: [],
            sessionRebuilt: false,
            meetingModeAfter: 'normal',
            detail: 'this voice assistant cannot change mode during a meeting',
        }),
        IDENTITY,
    );
    assert.match(declined, /result=declined/);
    assert.match(declined, /meetingMode=normal\(unchanged\)/, 'a refused request must not report a change');
});

test('a meeting-supplied name stays inside its own log line', () => {
    // Sender names come from the meeting platform's chat. Reducing them to one
    // bounded line keeps one toggle to one record.
    const line = formatAssistantToggleLog(
        { meetingMode: 'translator', route: 'chat', requestedBy: `Alice\nresult=applied vp=other\t"x"` },
        outcome(),
        IDENTITY,
    );
    assert.equal(line.split('\n').length, 1);
    assert.equal(line.match(/result=/g)?.length, 1);
    assert.equal(line.match(/vp=/g)?.length, 1);

    const long = formatAssistantToggleLog(
        { meetingMode: 'translator', route: 'chat', requestedBy: 'N'.repeat(500) },
        outcome(),
        IDENTITY,
    );
    assert.match(long, /by="N{80}"/);
});
