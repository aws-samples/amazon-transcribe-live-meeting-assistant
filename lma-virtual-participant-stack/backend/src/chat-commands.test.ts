/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The pause, resume and help chat commands (`details.pauseCommand`,
 * `details.startCommand`, `details.helpCommand`) act only on a message that is
 * the "LMA" addressee plus the command and nothing else — the same rule the
 * dismissal and assistant-mode commands follow — on every platform. Also the
 * `LMA help` reply itself, and the default introduction message.
 *
 * What is NOT covered here: what each platform's DOM scraper actually hands the
 * node side in a live meeting, and whether each platform's chat renders the
 * help reply as sent. Zoom's aria-label split is tested below because it is
 * node-side; the Teams, Webex and Chime scrapers run in the page, and are
 * relied on here only to deliver the same string matchesEndCommand already
 * matches against.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
    DEFAULT_INTRO_MESSAGE,
    END_COMMAND_VERBS,
    details,
    matchesChatCommand,
    matchesEndCommand,
} from './details.js';
import { ASSISTANT_CHAT_COMMANDS, parseAssistantChatCommand } from './assistant-toggles.js';
import { allChatHelpMessages, chatHelpMessages } from './chat-help.js';
import { parseChatQuestion } from './chat-ask.js';
import { splitZoomChatLabel } from './zoom.js';

const NOVA_LIKE = { isEnabled: () => true, supportsModeChanges: () => true };
const ELEVENLABS_LIKE = { isEnabled: () => true, supportsModeChanges: () => false };
const NO_ASSISTANT = { isEnabled: () => false, supportsModeChanges: () => false };

// ---------------------------------------------------------------------------
// The matcher
// ---------------------------------------------------------------------------

test('the pause command matches as the whole message addressed to LMA, in any case', () => {
    for (const message of ['LMA pause', 'lma PAUSE', '@LMA, PAUSE!', 'pause LMA', 'lma: Pause', '  LMA pause  ', 'LMA pause?']) {
        assert.equal(matchesChatCommand(message, details.pauseCommand), true, `no longer pauses: "${message}"`);
    }
});

test('the resume command matches as the whole message addressed to LMA, in any case', () => {
    for (const message of ['LMA start', 'LMA Start.', '@LMA START', 'start LMA', 'lma start...']) {
        assert.equal(matchesChatCommand(message, details.startCommand), true, `no longer resumes: "${message}"`);
    }
});

test('the help command matches as the whole message addressed to LMA, in any case', () => {
    for (const message of ['LMA help', '@LMA help', 'help LMA', 'LMA, HELP?', 'lma Help!']) {
        assert.equal(matchesChatCommand(message, details.helpCommand), true, `no longer answers help: "${message}"`);
    }
});

test('a command without the LMA addressee is ordinary chat', () => {
    for (const [message, command] of [
        ['PAUSE', details.pauseCommand],
        ['pause', details.pauseCommand],
        ['Pause.', details.pauseCommand],
        ['START', details.startCommand],
        ['start', details.startCommand],
        ['HELP', details.helpCommand],
        ['help?', details.helpCommand],
        ['can someone help', details.helpCommand],
    ] as const) {
        assert.equal(matchesChatCommand(message, command), false, `acts without the addressee: "${message}"`);
    }
});

test('a message that only contains the pause command is ordinary chat', () => {
    for (const message of [
        'PAUSED',
        'LMA PAUSED',
        'LMA UNPAUSE',
        "please don't PAUSE LMA",
        'LMA PAUSE the recording',
        'Type "LMA pause" to stop the transcript',
        'LMA PAUSE now',
        'LMA',
        '',
        '   ',
        '!!!',
    ]) {
        assert.equal(matchesChatCommand(message, details.pauseCommand), false, `pauses on prose: "${message}"`);
    }
});

test('a message that only contains the resume command is ordinary chat', () => {
    for (const message of ['LMA STARTED', 'LMA RESTART', "let's START LMA now", 'LMA START over', 'LMA START please']) {
        assert.equal(matchesChatCommand(message, details.startCommand), false, `resumes on prose: "${message}"`);
    }
});

test('a message that only contains the help command is ordinary chat', () => {
    for (const message of ['LMA helped a lot', 'can LMA help us', 'LMA help me with this', 'LMA helpful']) {
        assert.equal(matchesChatCommand(message, details.helpCommand), false, `answers help on prose: "${message}"`);
    }
});

test('an empty command never matches', () => {
    assert.equal(matchesChatCommand('LMA pause', ''), false);
    assert.equal(matchesChatCommand('', ''), false);
    assert.equal(matchesChatCommand('LMA', ''), false);
});

// ---------------------------------------------------------------------------
// The VP's own chat messages cannot trigger any command
// ---------------------------------------------------------------------------

/** Every chat command a message could trigger, by name, for the assertion message. */
function commandsTriggeredBy(message: string): string[] {
    const triggered: string[] = [];
    if (matchesEndCommand(message)) triggered.push('end');
    if (parseAssistantChatCommand(message)) triggered.push('assistant-mode');
    if (matchesChatCommand(message, details.pauseCommand)) triggered.push('pause');
    if (matchesChatCommand(message, details.startCommand)) triggered.push('start');
    if (matchesChatCommand(message, details.helpCommand)) triggered.push('help');
    if (parseChatQuestion(message, true)) triggered.push('ask');
    return triggered;
}

test("the VP's own introduction, status and help messages trigger no command", () => {
    // Several scrapers read the VP's own outgoing chat back (Teams polls every
    // rendered row), so a default message that matched would act on the VP.
    const own = [
        ...details.introMessages,
        ...details.startMessages,
        ...details.pauseMessages,
        ...details.exitMessages,
        ...allChatHelpMessages(),
    ];
    assert.ok(own.length >= 6);
    for (const message of own) {
        assert.deepEqual(commandsTriggeredBy(message), [], `own message triggers a command: "${message}"`);
    }
});

test('an introduction message that explains the commands does not trigger them', () => {
    const explained =
        'Hello. I am an AI Live Meeting Assistant (LMA). Type "LMA pause" to pause the transcript, ' +
        '"LMA start" to resume it, "LMA help" for the list, or "LMA leave" to dismiss me.';
    assert.deepEqual(commandsTriggeredBy(explained), []);
});

// ---------------------------------------------------------------------------
// The help reply
// ---------------------------------------------------------------------------

test('every command the help reply lists is accepted by its matcher', () => {
    // Pulled back out of the reply text, so a reply that drifts from the
    // matchers fails here rather than in a meeting.
    const listed = [...new Set(allChatHelpMessages()
        .join(' ')
        .match(/"LMA [^"]+"/g)!
        .map((quoted) => quoted.slice(1, -1)))];
    // leave, pause, start, ask and help, plus one canonical form per mode command.
    assert.equal(listed.length, 5 + ASSISTANT_CHAT_COMMANDS.length);
    for (const command of listed) {
        assert.ok(
            commandsTriggeredBy(command).length === 1,
            `the help reply lists "${command}", which ${commandsTriggeredBy(command).length ? 'triggers more than one command' : 'no matcher accepts'}`,
        );
    }
});

test('the help reply lists one canonical form of every command', () => {
    const text = chatHelpMessages(NOVA_LIKE).join(' ');
    for (const command of [
        END_COMMAND_VERBS[0],
        details.pauseCommand,
        details.startCommand,
        details.helpCommand,
        ...ASSISTANT_CHAT_COMMANDS.map((entry) => entry.phrases[0]),
    ]) {
        assert.ok(text.includes(`"LMA ${command.toLowerCase()}"`), `the help reply leaves out "LMA ${command}"`);
    }
    // Canonical forms only: no aliases of the mode commands.
    for (const entry of ASSISTANT_CHAT_COMMANDS) {
        for (const alias of entry.phrases.slice(1)) {
            assert.ok(!text.includes(`"LMA ${alias}"`), `the help reply lists the alias "LMA ${alias}"`);
        }
    }
    assert.ok(!text.includes('"LMA end"'), 'the help reply lists an alias of "LMA leave"');
});

test('the help reply names listen mode "LMA listen", and the configuration name "LMA group" works unlisted', () => {
    const text = chatHelpMessages(NOVA_LIKE).join(' ');
    assert.ok(text.includes('"LMA listen"'));
    assert.ok(!text.includes('group'), 'the help reply still mentions group');
    assert.equal(parseAssistantChatCommand('LMA group')?.meetingMode, 'group');
    assert.equal(parseAssistantChatCommand('LMA listen')?.meetingMode, 'group');
});

test('the help reply lists LMA ask only when a meeting assistant can answer it', () => {
    assert.ok(chatHelpMessages(NO_ASSISTANT, true).join(' ').includes('"LMA ask <question>"'));
    assert.ok(!chatHelpMessages(NOVA_LIKE, false).join(' ').includes('LMA ask'));
});

test('the help reply leaves out the mode commands when no assistant can change mode', () => {
    for (const assistant of [NO_ASSISTANT, ELEVENLABS_LIKE]) {
        const text = chatHelpMessages(assistant).join(' ');
        for (const entry of ASSISTANT_CHAT_COMMANDS) {
            assert.ok(!text.includes(`"LMA ${entry.phrases[0]}"`), `"LMA ${entry.phrases[0]}" listed with no assistant to act on it`);
        }
        assert.ok(text.includes('"LMA leave"'));
        assert.ok(text.includes('"LMA help"'));
    }
    assert.equal(chatHelpMessages(NO_ASSISTANT).length, 1);
    assert.equal(chatHelpMessages(NOVA_LIKE).length, 3);
});

test('every help message is one short line', () => {
    // Chime and Webex post by typing, where a newline is an Enter key and would
    // send the reply in pieces. Kept to about the length of the introduction
    // message, which every platform already posts.
    for (const message of allChatHelpMessages()) {
        assert.doesNotMatch(message, /[\r\n]/, `help message spans lines: "${message}"`);
        assert.ok(message.length <= 250, `help message is ${message.length} characters: "${message}"`);
    }
});

// ---------------------------------------------------------------------------
// The default introduction message
// ---------------------------------------------------------------------------

test('the default introduction message points at LMA leave and LMA help', () => {
    assert.match(DEFAULT_INTRO_MESSAGE, /"LMA leave"/);
    assert.match(DEFAULT_INTRO_MESSAGE, /"LMA help"/);
    assert.match(DEFAULT_INTRO_MESSAGE, /\{LMA_USER\}/);
});

test('the default introduction message matches the IntroMessage stack parameter default', () => {
    // The stack parameter is what a deployed VP receives as INTRO_MESSAGE, so it
    // is the default that matters; the one in details.ts covers a VP run without
    // it. The parameter is a folded block scalar (`>-`), whose lines join with
    // single spaces.
    const template = readSource('../../template.yaml');
    const block = template.match(/\n {2}IntroMessage:\n {4}Type: String\n {4}Default: >-\n((?: {6}.*\n)+)/);
    assert.ok(block, 'could not find the IntroMessage parameter in template.yaml — this test needs updating');
    const folded = block![1]
        .split('\n')
        .map((line) => line.trim())
        .filter((line) => line.length > 0)
        .join(' ');
    assert.equal(folded, DEFAULT_INTRO_MESSAGE);
});

// ---------------------------------------------------------------------------
// The vocabularies stay disjoint
// ---------------------------------------------------------------------------

test('the pause, resume and help commands each trigger exactly one command', () => {
    assert.deepEqual(commandsTriggeredBy('LMA pause'), ['pause']);
    assert.deepEqual(commandsTriggeredBy('LMA start'), ['start']);
    assert.deepEqual(commandsTriggeredBy('LMA help'), ['help']);
});

// ---------------------------------------------------------------------------
// Zoom: the command is matched against the body, not the aria-label
// ---------------------------------------------------------------------------

test('a Zoom chat label in the colon form is split into sender and body', () => {
    assert.deepEqual(splitZoomChatLabel('Alice Smith to Everyone: PAUSE'), { sender: 'Alice Smith', body: 'PAUSE' });
});

test('a Zoom chat label in the timestamp form is split into sender and body', () => {
    assert.deepEqual(splitZoomChatLabel('Alice Smith to Everyone, 10:42 AM, PAUSE'), {
        sender: 'Alice Smith',
        body: 'PAUSE',
    });
    assert.deepEqual(splitZoomChatLabel('Bob to Everyone, 14:05, LMA start'), { sender: 'Bob', body: 'LMA start' });
});

test('a Zoom label whose body has its own colon keeps the whole body', () => {
    assert.deepEqual(splitZoomChatLabel('Alice to Everyone: note: PAUSE later'), {
        sender: 'Alice',
        body: 'note: PAUSE later',
    });
});

test('a Zoom label in neither shape is returned whole with no sender', () => {
    assert.deepEqual(splitZoomChatLabel('PAUSE'), { sender: null, body: 'PAUSE' });
});

test('a Zoom pause command matches through the label split, and prose around it does not', () => {
    const matches = (label: string) => matchesChatCommand(splitZoomChatLabel(label).body, details.pauseCommand);
    assert.equal(matches('Alice to Everyone: LMA PAUSE'), true);
    assert.equal(matches('Alice to Everyone, 10:42 AM, lma pause'), true);
    assert.equal(matches('Alice to Everyone: PAUSE'), false);
    assert.equal(matches('Alice to Everyone: LMA PAUSED for lunch'), false);
    // A sender whose name is the command word does not make their message one.
    assert.equal(matches('LMA PAUSE to Everyone: see you after the break'), false);
});

// ---------------------------------------------------------------------------
// Every platform uses the shared matcher
// ---------------------------------------------------------------------------

function readSource(file: string): string {
    // Resolved from this module so the suite works whether it is run from dist/
    // (the normal `node --test dist/*.test.js`) or from src/ under ts-node.
    const here = new URL('.', import.meta.url);
    for (const url of [new URL(`../src/${file}`, here), new URL(`./${file}`, here)]) {
        try {
            return readFileSync(url, 'utf8');
        } catch {
            /* try the next location */
        }
    }
    assert.fail(`could not read ${file} — the path in this test needs updating`);
}

test('every platform handler matches pause, resume and help with matchesChatCommand', () => {
    for (const file of ['zoom.ts', 'zoom-sdk.ts', 'teams.ts', 'teams-sdk.ts', 'webex.ts', 'chime.ts']) {
        const src = readSource(file);
        assert.match(
            src,
            /chatHelpMessages\(voiceAssistant\)/,
            `${file} does not answer the help command with chatHelpMessages(voiceAssistant)`,
        );
        for (const command of ['pauseCommand', 'startCommand', 'helpCommand']) {
            assert.match(
                src,
                new RegExp(`matchesChatCommand\\(\\w+, details\\.${command}\\)`),
                `${file} does not call matchesChatCommand(<identifier>, details.${command}) — either the ` +
                    'handler stopped using the shared matcher, or the call was reformatted and this regex needs updating',
            );
            assert.doesNotMatch(
                src,
                new RegExp(`(includes\\(details\\.${command}\\)|=== details\\.${command})`),
                `${file} still matches details.${command} some other way`,
            );
        }
    }
});

test('every platform handler answers chat questions with startChatAnswer under its own name', () => {
    for (const [file, platform] of [
        ['zoom.ts', 'zoom'],
        ['zoom-sdk.ts', 'zoom-sdk'],
        ['teams.ts', 'teams'],
        ['teams-sdk.ts', 'teams-sdk'],
        ['webex.ts', 'webex'],
        ['chime.ts', 'chime'],
    ]) {
        const src = readSource(file);
        assert.match(src, /parseChatQuestion\(/, `${file} does not parse chat questions`);
        // Started beside the command chain rather than as a branch of it, so a
        // question still reaches the branch that records meeting chat.
        assert.ok(!/else if \(chatQuestion/.test(src), `${file} answers a question instead of recording it`);
        assert.ok(src.includes(`'${platform}', (messages) =>`), `${file} does not answer chat questions as '${platform}'`);
    }
});
