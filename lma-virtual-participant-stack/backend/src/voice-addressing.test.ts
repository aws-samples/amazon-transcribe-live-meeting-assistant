/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Addressing the voice assistant by name (voice-addressing.ts): which
 * utterances count as put to it, and which only mention it. Both transcripts
 * the agent reads are covered: the participants' own (punctuated) and Nova
 * Sonic's (lowercase, no punctuation).
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    addressedByName,
    addressesAssistantByWakeOrName,
    assistantNameFromPrompt,
    assistantNames,
} from './voice-addressing.js';

const WAKE = ['hey alex', 'ok alex', 'hi alex', 'hello alex'];
const NAMES = ['alex'];

test('the name is read from the system prompt that tells the model who it is', () => {
    assert.equal(assistantNameFromPrompt('You are Alex, an AI meeting assistant. Be concise and helpful.'), 'alex');
    assert.equal(assistantNameFromPrompt('Your name is Morgan. You help with meetings.'), 'morgan');
    assert.equal(assistantNameFromPrompt("You're Sam, the team's assistant."), 'sam');
    assert.equal(assistantNameFromPrompt('You are an AI meeting assistant.'), null);
    assert.equal(assistantNameFromPrompt('You are going to help.'), null);
    assert.equal(assistantNameFromPrompt(''), null);
});

test('the assistant answers to the prompt name and the wake phrases name, once each', () => {
    assert.deepEqual(assistantNames('You are Alex, an AI meeting assistant.', WAKE), ['alex']);
    assert.deepEqual(assistantNames('You are Morgan, an AI meeting assistant.', WAKE), ['morgan', 'alex']);
    assert.deepEqual(assistantNames('You are an AI meeting assistant.', ['hey alex', 'ok computer']), []);
});

test('a name that opens the utterance addresses the assistant', () => {
    for (const said of [
        'Alex, what is next on the agenda?',
        'alex what is next on the agenda',
        'um alex summarize the last ten minutes',
        'So, Alex, any action items?',
        'Alex?',
        'alex',
    ]) {
        assert.equal(addressedByName(said, NAMES), true, said);
    }
});

test('a name followed by a comma or a request word addresses the assistant', () => {
    for (const said of [
        'Awesome. Alex, can you tell me the weather this afternoon in Herndon?',
        // Nova Sonic's own transcript of the live meeting's utterance.
        "let's see if alex awesome alex can you",
        'okay great thanks alex could you look that up',
        'and now alex please summarize',
        'right so alex what did we decide',
    ]) {
        assert.equal(addressedByName(said, NAMES), true, said);
    }
});

test('a name closing a sentence after a comma addresses the assistant', () => {
    for (const said of [
        "What's the weather this afternoon, Alex?",
        'Thanks, Alex.',
        'Can you look that up, Alex? I need it for the slide.',
    ]) {
        assert.equal(addressedByName(said, NAMES), true, said);
    }
});

test('a mention of the name is not addressed to the assistant', () => {
    for (const said of [
        'I told Alex about the budget yesterday.',
        'i told alex about the budget yesterday',
        'Alex is presenting next.',
        'alex said we should ship on friday',
        "That was Alex's idea.",
        "alex's slides are in the deck",
        'We should ask Alex what he thinks.',
        'I was talking to Alex, and he agreed.',
        'Let me check with alex can you hear me Sam',
        'Alex will present the roadmap.',
        // Nova Sonic's unpunctuated turns: a name opening a statement.
        'alex presented the roadmap',
        'alex sent the deck this morning',
        'alex the weather in herndon',
        'can you check the weather alex',
        'nothing to do with anyone named here',
    ]) {
        assert.equal(addressedByName(said, NAMES), false, said);
    }
});

test('a hyphenated name from the prompt is matched as one word', () => {
    const names = assistantNames('You are Mary-Kate, an AI meeting assistant.', []);
    assert.deepEqual(names, ['mary-kate']);
    assert.equal(addressedByName('Mary-Kate, what is next?', names), true);
    assert.equal(addressedByName('mary kate what is next', names), false);
});

test('a wake phrase addresses the assistant as before, and no name means only the wake phrase does', () => {
    assert.equal(addressesAssistantByWakeOrName('hey alex what is next', WAKE, NAMES), true);
    assert.equal(addressesAssistantByWakeOrName('Okay Alex, what is next?', WAKE, NAMES), true);
    assert.equal(addressesAssistantByWakeOrName('Alex, what is next?', WAKE, []), false);
    assert.equal(addressesAssistantByWakeOrName('I told Alex about it', WAKE, NAMES), false);
});
