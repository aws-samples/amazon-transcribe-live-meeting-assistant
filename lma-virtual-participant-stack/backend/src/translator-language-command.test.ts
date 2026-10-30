/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * `LMA translate <language>`: choosing the translator's language pair for one
 * meeting from the chat.
 *
 * The pair is (the meeting's own language) and (the language asked for). The
 * language is checked against the closed list Nova Sonic supports, and the
 * choice is never written to the configuration store.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    applyAssistantChatCommand,
    createActivationRestore,
    formatAssistantToggleLog,
    formatChatRefusalLog,
    parseAssistantChatCommand,
    type AssistantReconfigureOutcome,
    type AssistantReconfigureRequest,
} from './assistant-toggles.js';
import { meetingLanguage, resolveTranslatorLanguage } from './translator-languages.js';
import { NovaAgent } from './nova-agent.js';
import { NoOpAgent } from './noop-agent.js';
import { details } from './details.js';

const captured: string[] = [];
console.log = (...args: unknown[]) => {
    captured.push(args.map(String).join(' '));
};

// ---------------------------------------------------------------------------
// The language list
// ---------------------------------------------------------------------------

test('a supported language is recognised in English, in itself, and with or without accents', () => {
    for (const [typed, name] of [
        ['Spanish', 'Spanish'],
        ['spanish', 'Spanish'],
        ['Español', 'Spanish'],
        ['espanol', 'Spanish'],
        ['FRANÇAIS', 'French'],
        ['Deutsch', 'German'],
        ['italiano', 'Italian'],
        ['Português', 'Portuguese'],
        ['brazilian portuguese', 'Portuguese'],
        ['Hindi', 'Hindi'],
        ['english', 'English'],
    ] as const) {
        assert.equal(resolveTranslatorLanguage(typed)?.name, name, `"${typed}"`);
    }
    for (const typed of ['Klingon', 'Japanese', 'the slides', '']) {
        assert.equal(resolveTranslatorLanguage(typed), null, `"${typed}" is not a supported language`);
    }
});

test("the meeting's language comes from the Transcribe language code, or the preferred one under identification", () => {
    assert.equal(meetingLanguage('en-US')?.name, 'English');
    assert.equal(meetingLanguage('fr-CA')?.name, 'French');
    assert.equal(meetingLanguage('pt-BR')?.name, 'Portuguese');
    assert.equal(meetingLanguage('identify-language', 'es-US')?.name, 'Spanish');
    assert.equal(meetingLanguage('identify-multiple-languages', '')?.name, undefined);
    assert.equal(meetingLanguage('ja-JP'), null, 'a meeting in an unsupported language names no pair half');
});

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

test('LMA translate <language> asks for translator mode with that language', () => {
    for (const message of [
        'LMA translate French',
        'LMA translate to French',
        'LMA translate into french',
        '@LMA, translate Français!',
        'LMA translator French',
        'LMA interpret in French',
    ]) {
        assert.deepEqual(
            parseAssistantChatCommand(message),
            { meetingMode: 'translator', activationMode: 'always_active', translatorLanguage: 'French' },
            `"${message}"`,
        );
    }
});

test('naming two languages is answered with the one-language form', async () => {
    for (const message of ['LMA translate English to French', 'LMA translate French and Spanish']) {
        const command = parseAssistantChatCommand(message)!;
        assert.equal(command.twoLanguages, true, message);
        const reply = await applyAssistantChatCommand(recordingAssistant(), command, null, createActivationRestore());
        assert.match(reply[0], /name one language/);
    }
});

test('a configured pair typed in another case is the same pair', async () => {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'base',
        activationMode: 'always_active',
        meetingMode: 'translator',
        translatorLanguageA: 'english',
        translatorLanguageB: 'spanish',
    });
    const outcome = await agent.reconfigure({
        meetingMode: 'translator',
        activationMode: 'always_active',
        translatorLanguages: { a: 'English', b: 'Spanish' },
        route: 'chat',
    });
    assert.equal(outcome.result, 'unchanged');
});

test('bare LMA translate carries no language, so the configured pair is kept', () => {
    assert.deepEqual(parseAssistantChatCommand('LMA translate'), {
        meetingMode: 'translator',
        activationMode: 'always_active',
    });
});

test('one unknown word is answered; several are ordinary chat', () => {
    assert.deepEqual(parseAssistantChatCommand('LMA translate Klingon'), { unsupportedLanguage: 'klingon' });
    assert.equal(parseAssistantChatCommand('LMA translate the slides'), null);
    assert.equal(parseAssistantChatCommand('can LMA translate French'), null);
    for (const message of ['LMA translate to', 'LMA translate in', 'LMA translate it', 'LMA translate this', 'LMA translate please']) {
        assert.equal(parseAssistantChatCommand(message), null, `"${message}" is chat, not a language`);
    }
});

// ---------------------------------------------------------------------------
// The chat flow
// ---------------------------------------------------------------------------

function recordingAssistant(result: Partial<AssistantReconfigureOutcome> = {}) {
    const requests: AssistantReconfigureRequest[] = [];
    return {
        requests,
        reconfigure: async (request: AssistantReconfigureRequest): Promise<AssistantReconfigureOutcome> => {
            requests.push(request);
            return {
                result: 'applied',
                changed: ['meetingMode', 'translatorLanguages'],
                sessionRebuilt: true,
                meetingModeBefore: 'normal',
                meetingModeAfter: 'translator',
                activationModeBefore: 'always_active',
                activationModeAfter: 'always_active',
                translatorLanguagesAfter: { a: 'English', b: request.translatorLanguages?.b || 'Spanish' },
                ...result,
            };
        },
    };
}

async function withTranscribeLanguage<T>(code: string, preferred: string, run: () => Promise<T>): Promise<T> {
    const before = [details.transcribeLanguageCode, details.transcribePreferredLanguage];
    details.transcribeLanguageCode = code;
    details.transcribePreferredLanguage = preferred;
    try {
        return await run();
    } finally {
        [details.transcribeLanguageCode, details.transcribePreferredLanguage] = before;
    }
}

test("the pair is the meeting's language and the one asked for, and the reply says it is for this meeting only", async () => {
    const assistant = recordingAssistant();
    const reply = await withTranscribeLanguage('en-US', '', () =>
        applyAssistantChatCommand(
            assistant,
            parseAssistantChatCommand('LMA translate French')!,
            'Guest',
            createActivationRestore(),
        ),
    );
    assert.deepEqual(assistant.requests[0].translatorLanguages, { a: 'English', b: 'French' });
    assert.equal(reply.length, 1);
    assert.match(reply[0], /switched to translator mode \(English and French\)/);
    assert.match(reply[0], /this meeting only/);
    assert.doesNotMatch(reply[0], /\n/);
});

test('under language identification with no preferred language, the provider picks the first half', async () => {
    const assistant = recordingAssistant();
    await withTranscribeLanguage('identify-language', '', () =>
        applyAssistantChatCommand(assistant, parseAssistantChatCommand('LMA translate German')!, null, createActivationRestore()),
    );
    assert.deepEqual(assistant.requests[0].translatorLanguages, { a: undefined, b: 'German' });
});

test("asking for the meeting's own language is answered without changing anything", async () => {
    const assistant = recordingAssistant();
    const reply = await withTranscribeLanguage('fr-FR', '', () =>
        applyAssistantChatCommand(assistant, parseAssistantChatCommand('LMA translate French')!, null, createActivationRestore()),
    );
    assert.equal(assistant.requests.length, 0);
    assert.match(reply[0], /French is the language this meeting is transcribed in/);
});

test('an unsupported language is answered with the supported list and changes nothing', async () => {
    const assistant = recordingAssistant();
    const reply = await applyAssistantChatCommand(
        assistant,
        parseAssistantChatCommand('LMA translate Klingon')!,
        null,
        createActivationRestore(),
    );
    assert.equal(assistant.requests.length, 0);
    assert.match(reply[0], /can't translate into "klingon"/);
    assert.match(reply[0], /English, Spanish, French, German, Italian, Portuguese, Hindi/);
});

test('bare LMA translate sends no language pair and adds no meeting-only note', async () => {
    const assistant = recordingAssistant({ changed: ['meetingMode'] });
    const reply = await applyAssistantChatCommand(
        assistant,
        parseAssistantChatCommand('LMA translate')!,
        null,
        createActivationRestore(),
    );
    assert.equal(assistant.requests[0].translatorLanguages, undefined);
    assert.doesNotMatch(reply[0], /this meeting only/);
});

test('the toggle log line names the pair when one was asked for', () => {
    const line = formatAssistantToggleLog(
        { meetingMode: 'translator', translatorLanguages: { a: 'English', b: 'French' }, route: 'chat' },
        {
            result: 'applied',
            changed: ['meetingMode', 'translatorLanguages'],
            sessionRebuilt: true,
            meetingModeBefore: 'normal',
            meetingModeAfter: 'translator',
            activationModeBefore: 'always_active',
            activationModeAfter: 'always_active',
            translatorLanguagesAfter: { a: 'English', b: 'French' },
        },
        { vpId: 'vp', callId: 'call' },
    );
    assert.match(line, /translator="English<->French"$/);
});

// ---------------------------------------------------------------------------
// The Nova provider
// ---------------------------------------------------------------------------

function nova(meetingMode: 'normal' | 'translator' = 'normal'): NovaAgent {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'base',
        activationMode: 'always_active',
        meetingMode,
        translatorLanguageA: 'English',
        translatorLanguageB: 'Spanish',
    });
    const inner = agent as any;
    inner.createSession = async () => {};
    inner.closeSession = async () => {};
    inner.refreshSession = async () => {};
    return agent;
}

function prompt(agent: NovaAgent): string {
    return (agent as any).buildSystemPromptContent();
}

test('Nova switches into translator mode with the requested pair and writes it into the prompt', async () => {
    const agent = nova();
    const outcome = await agent.reconfigure({
        meetingMode: 'translator',
        activationMode: 'always_active',
        translatorLanguages: { a: 'English', b: 'French' },
        route: 'chat',
    });
    assert.equal(outcome.result, 'applied');
    assert.deepEqual(outcome.changed, ['meetingMode', 'translatorLanguages']);
    assert.deepEqual(outcome.translatorLanguagesAfter, { a: 'English', b: 'French' });
    assert.match(prompt(agent), /interpreter between English and French/);
});

test('changing only the pair of a running translator rebuilds it with the new pair', async () => {
    const agent = nova('translator');
    const outcome = await agent.reconfigure({
        meetingMode: 'translator',
        activationMode: 'always_active',
        translatorLanguages: { a: 'English', b: 'German' },
        route: 'chat',
    });
    assert.equal(outcome.result, 'applied');
    assert.deepEqual(outcome.changed, ['translatorLanguages']);
    assert.match(prompt(agent), /between English and German/);
});

test('the same pair again is unchanged and says which pair is in use', async () => {
    const agent = nova('translator');
    const outcome = await agent.reconfigure({
        meetingMode: 'translator',
        activationMode: 'always_active',
        translatorLanguages: { a: 'English', b: 'Spanish' },
        route: 'chat',
    });
    assert.equal(outcome.result, 'unchanged');
    assert.match(outcome.detail!, /already translating between English and Spanish/);
});

test('with no meeting language, the configured first half is kept, or the second when the first was asked for', async () => {
    const first = nova();
    await first.reconfigure({ meetingMode: 'translator', translatorLanguages: { b: 'French' }, activationMode: 'always_active', route: 'chat' });
    assert.match(prompt(first), /between English and French/);

    const second = nova();
    await second.reconfigure({ meetingMode: 'translator', translatorLanguages: { b: 'English' }, activationMode: 'always_active', route: 'chat' });
    assert.match(prompt(second), /between Spanish and English/);
});

test('a pair of one language twice is declined', async () => {
    const agent = nova();
    const outcome = await agent.reconfigure({
        meetingMode: 'translator',
        translatorLanguages: { a: 'French', b: 'french' },
        activationMode: 'always_active',
        route: 'chat',
    });
    assert.equal(outcome.result, 'declined');
    assert.match(outcome.detail!, /needs two different languages/);
    assert.equal((agent as any).meetingMode, 'normal');
});

test('with an assistant that cannot change mode, a language command is declined by the assistant itself', async () => {
    // Not answered with a language list or a suggestion that would then be
    // declined too.
    for (const message of ['LMA translate Klingon', 'LMA translate French']) {
        const reply = await withTranscribeLanguage('en-US', '', () =>
            applyAssistantChatCommand(new NoOpAgent(), parseAssistantChatCommand(message)!, 'Alice', createActivationRestore()),
        );
        assert.deepEqual(reply, ['Live Meeting Assistant: the voice assistant is not enabled for this meeting.'], message);
    }
});

test('a refusal made before the provider is logged with the same identifying fields', async () => {
    captured.length = 0;
    await withTranscribeLanguage('en-US', '', () =>
        applyAssistantChatCommand(recordingAssistant(), parseAssistantChatCommand('LMA translate English')!, 'Alice', createActivationRestore()),
    );
    const line = captured.find((l) => l.includes('[assistant-toggle]'))!;
    assert.match(line, /vp=\S+ call=\S+ route=chat by="Alice" result=declined detail="English is the meeting's own language"/);
    assert.equal(
        formatChatRefusalLog('Bob', 'unsupported translator language klingon', { vpId: 'vp', callId: 'call' }),
        '[assistant-toggle] vp=vp call=call route=chat by="Bob" result=declined detail="unsupported translator language klingon"',
    );
});

test('an unchanged pair still logs the pair in use', async () => {
    captured.length = 0;
    const agent = nova('translator');
    await agent.reconfigure({
        meetingMode: 'translator',
        activationMode: 'always_active',
        translatorLanguages: { a: 'English', b: 'Spanish' },
        route: 'chat',
    });
    const line = captured.find((l) => l.includes('[assistant-toggle]'))!;
    assert.match(line, /translator="English<->Spanish"\(unchanged\)/);
});
