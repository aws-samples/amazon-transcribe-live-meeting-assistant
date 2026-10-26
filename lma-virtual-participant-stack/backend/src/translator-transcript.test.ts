/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A translator meeting keeps a continuous transcript: a participant row the
 * assistant starts answering is closed rather than left in progress, and the
 * assistant's translation is written from its own text.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import {
    OpenPartialTracker,
    assistantRowSpan,
    resultAfter,
    assistantTurnText,
    onAssistantTurn,
    publishAssistantTurn,
} from './translator-transcript.js';

function result(id: string, text: string, isPartial: boolean): any {
    const words = text.split(' ');
    return {
        ResultId: id,
        IsPartial: isPartial,
        Alternatives: [
            {
                Transcript: text,
                Items: words.map((word, i) => ({
                    Type: 'pronunciation',
                    Content: word,
                    StartTime: i * 0.4,
                    EndTime: i * 0.4 + 0.3,
                })),
            },
        ],
    };
}

test('the last written partial is handed back once, stamped final, with what it was sent with', () => {
    const tracker = new OpenPartialTracker();
    tracker.noteSent(result('r1', 'The weather', true), 'Bob', 12);
    tracker.noteSent(result('r1', 'The weather today is very dry and sunny', true), 'Bob', 12);

    const closed = tracker.close();
    assert.ok(closed);
    assert.equal(closed.result.IsPartial, false);
    assert.equal(closed.result.ResultId, 'r1');
    assert.equal(closed.result.Alternatives[0].Transcript, 'The weather today is very dry and sunny');
    assert.equal(closed.speaker, 'Bob');
    assert.equal(closed.timeOffsetSeconds, 12);
    assert.equal(tracker.close(), null);
});

test('a result whose final was written leaves nothing to close', () => {
    const tracker = new OpenPartialTracker();
    tracker.noteSent(result('r1', 'Hello there', true), 'Bob', 0);
    tracker.noteSent(result('r1', 'Hello there.', false), 'Bob', 0);
    assert.equal(tracker.close(), null);
});

test('closing does not change the partial that was already written', () => {
    const tracker = new OpenPartialTracker();
    const partial = result('r1', 'Hello there', true);
    tracker.noteSent(partial, 'Bob', 0);
    tracker.close();
    assert.equal(partial.IsPartial, true);
});

test('later deliveries for a closed row are recognised until its own final', () => {
    const tracker = new OpenPartialTracker();
    tracker.noteSent(result('r1', 'The weather today', true), 'Bob', 0);
    tracker.close();

    assert.equal(tracker.isClosed(result('r1', 'The weather today in Herndon El clima', true)), true);
    assert.equal(tracker.isClosed(result('r1', 'The weather today in Herndon El clima hoy', false)), true);
    // Its final ended the result; the id is not held for ever.
    assert.equal(tracker.isClosed(result('r1', 'anything', true)), false);
    assert.equal(tracker.isClosed(result('r2', 'Next utterance', true)), false);
});

test('a closed row reports where it ends, and is forgotten on its final', () => {
    const tracker = new OpenPartialTracker();
    tracker.noteSent(result('r1', 'one two three', true), 'Bob', 0);
    tracker.close();
    // Three words at 0, 0.4, 0.8 (each 0.3 long): the row ends at 1.1.
    assert.equal(tracker.closedRowEnd(result('r1', 'one two three four', true)), 1.1);
    assert.equal(tracker.closedRowEnd(result('r1', 'one two three four five', false)), 1.1);
    assert.equal(tracker.closedRowEnd(result('r1', 'anything', false)), null);
});

test('the words after a point are split off as a row of their own', () => {
    const full = result('r1', 'The weather is dry El clima Thanks everyone', false);
    // Words start at 0, 0.4, ... 2.8; keep from the one at 2.4 ("Thanks").
    const rest = resultAfter(full, 2.3);
    assert.ok(rest);
    assert.equal(rest.ResultId, 'r1-after');
    assert.equal(rest.IsPartial, false);
    assert.equal(rest.Alternatives[0].Transcript, 'Thanks everyone');
    assert.equal(rest.Alternatives[0].Items.length, 2);
    assert.ok(Math.abs(rest.StartTime - 2.4) < 1e-9);
    // The original is untouched.
    assert.equal(full.ResultId, 'r1');
    assert.equal(full.Alternatives[0].Items.length, 8);
    assert.equal(resultAfter(full, 10), null);
});

test('punctuation stays attached when words are split off', () => {
    const r = {
        ResultId: 'r1',
        IsPartial: false,
        Alternatives: [
            {
                Transcript: 'Hola. Thanks, all.',
                Items: [
                    { Type: 'pronunciation', Content: 'Hola', StartTime: 0, EndTime: 0.3 },
                    { Type: 'punctuation', Content: '.' },
                    { Type: 'pronunciation', Content: 'Thanks', StartTime: 1, EndTime: 1.3 },
                    { Type: 'punctuation', Content: ',' },
                    { Type: 'pronunciation', Content: 'all', StartTime: 1.4, EndTime: 1.6 },
                    { Type: 'punctuation', Content: '.' },
                ],
            },
        ],
    };
    assert.equal(resultAfter(r, 0.9)?.Alternatives[0].Transcript, 'Thanks, all.');
});

test('a new session forgets the previous one', () => {
    const tracker = new OpenPartialTracker();
    tracker.noteSent(result('r1', 'Hello there', true), 'Bob', 0);
    tracker.reset();
    assert.equal(tracker.close(), null);

    tracker.noteSent(result('r2', 'Hello again', true), 'Bob', 0);
    tracker.close();
    tracker.reset();
    assert.equal(tracker.isClosed(result('r2', 'Hello again', false)), false);
});

test('a partial with no words is not kept open', () => {
    const tracker = new OpenPartialTracker();
    tracker.noteSent({ ResultId: 'r1', IsPartial: true, Alternatives: [{ Transcript: '', Items: [] }] }, 'Bob', 0);
    assert.equal(tracker.close(), null);
});

test('assistant turn text drops the provider interruption marker and extra whitespace', () => {
    assert.equal(
        assistantTurnText('{ "interrupted" : true }  El clima hoy en Herndon está muy seco.\n'),
        'El clima hoy en Herndon está muy seco.',
    );
    assert.equal(assistantTurnText('{"interrupted":true}'), '');
    assert.equal(assistantTurnText(''), '');
});

test('published assistant turns reach subscribers, and empty turns do not', () => {
    const seen: string[] = [];
    const unsubscribe = onAssistantTurn((text) => seen.push(text));
    publishAssistantTurn('  Hola a todos.  ');
    publishAssistantTurn('{ "interrupted" : true }');
    unsubscribe();
    publishAssistantTurn('After unsubscribing');
    assert.deepEqual(seen, ['Hola a todos.']);
});

test('a failing subscriber does not stop the others', () => {
    const seen: string[] = [];
    const a = onAssistantTurn(() => {
        throw new Error('boom');
    });
    const b = onAssistantTurn((text) => seen.push(text));
    publishAssistantTurn('Hola');
    a();
    b();
    assert.deepEqual(seen, ['Hola']);
});

test('an assistant row starts where its speech started and never before the previous row', () => {
    assert.deepEqual(assistantRowSpan(512.4, 507.0, 400), { start: 507.0, end: 512.4 });
    // A speech start older than the previous row belongs to an earlier turn.
    assert.deepEqual(assistantRowSpan(520, 507.0, 512.4), { start: 520, end: 520 });
    assert.deepEqual(assistantRowSpan(520, null, 512.4), { start: 520, end: 520 });
    // The clock is never allowed to place a row behind the previous one.
    assert.deepEqual(assistantRowSpan(510, null, 512.4), { start: 512.4, end: 512.4 });
});

// --- Wiring, asserted at the source level like scribe-asr-channels.test.ts ---

const read = (name: string): string =>
    readFileSync(new URL(`./${name}`, import.meta.url).pathname.replace('/dist/', '/src/'), 'utf8');
const scribe = read('scribe.ts');
const nova = read('nova-agent.ts');

function methodBody(src: string, name: string): string {
    const start = src.indexOf(name);
    assert.notEqual(start, -1, `${name} not found`);
    const rest = src.slice(start);
    const end = rest.slice(1).search(/\n {4}(private|public|async|\/\*\*)/);
    return end === -1 ? rest : rest.slice(0, end + 1);
}

test('a held-back result first closes the participant row it continues', () => {
    const events = methodBody(scribe, 'private async handleTranscriptEvents');
    const held = events.indexOf('if (suppressAgentTranscript) {');
    assert.notEqual(held, -1);
    const closeAt = events.indexOf('await closeOpenPartial()', held);
    const syncAt = events.indexOf('syncTranscriptSegmentState', held);
    assert.ok(closeAt > held && closeAt < syncAt, 'the row is closed before segment state moves on');
    assert.match(events, /this\.openPartials\.reset\(\)/);
    assert.match(events, /this\.openPartials\.noteSent\(forward, currentSpeaker, sessionOffsetSeconds\)/);
    assert.match(events, /this\.openPartials\.closedRowEnd\(result\)/);
    assert.match(events, /this\.speechAfterAssistant\(result, sessionOffsetSeconds, closedEnd\)/);
    const after = methodBody(scribe, 'private speechAfterAssistant');
    assert.match(after, /Math\.max\(this\.agentSpeechEndedAt - sessionOffsetSeconds, closedEnd\)/);
    const subscribe = methodBody(scribe, 'private subscribeToAssistantTurns');
    assert.match(subscribe, /on\('stopped'[\s\S]*silenceMs/);
});

test('a session that ends, cleanly or not, closes its open row before the timeline moves on', () => {
    const events = methodBody(scribe, 'private async handleTranscriptEvents');
    const fin = events.lastIndexOf('} finally {');
    assert.notEqual(fin, -1);
    const closeAt = events.indexOf('await closeOpenPartial()', fin);
    const offsetAt = events.indexOf('this.transcribeTimeOffsetSeconds = observedMaxEndTime', fin);
    assert.ok(closeAt > fin && closeAt < offsetAt);
});

test('assistant turns are written as the assistant only when nothing else transcribes its voice', () => {
    const needs = methodBody(scribe, 'private assistantTurnsNeedTranscript');
    assert.match(needs, /details\.meetingMode === 'translator'/);
    assert.match(needs, /!this\.agentSession \|\| this\.agentSession\.hasGivenUp/);
    const write = methodBody(scribe, 'private handleAssistantTurn');
    assert.match(write, /this\.assistantTurnsNeedTranscript\(\)/);
    assert.match(write, /channel: 'AGENT'/);
    assert.match(write, /isPartial: false/);
    assert.match(write, /lmaIdentity/);
    assert.doesNotMatch(write, /processTranscriptResult/);
});

test('Nova publishes each finished assistant turn', () => {
    assert.match(nova, /currentTurn\.role === 'ASSISTANT'\) \{[\s\S]{0,400}publishAssistantTurn\(currentTurn\.content\)/);
});
