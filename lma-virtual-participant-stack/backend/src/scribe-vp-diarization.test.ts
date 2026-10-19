/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The two wiring facts that cannot be observed behaviourally.
 *
 * Everything about what the writer EMITS is in kinesis-diarized-segments.ts,
 * driving the real module. Only these two live in `startTranscription`, which
 * needs a live meeting, a browser and Amazon Transcribe to reach.
 *
 * Source-text assertions are weak and this file is deliberately small because
 * of it: an earlier, larger version of it passed while the feature was
 * completely dead, while failing on a rename of a local variable. Each
 * assertion here strips comments first, so a commented-out line cannot satisfy
 * it -- which is exactly how the `ShowSpeakerLabel` check used to pass with the
 * request removed.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';

/** `scribe.ts` with comments removed, so a commented line proves nothing. */
const code = ((): string => {
    const raw = readFileSync(
        new URL('./scribe.ts', import.meta.url).pathname.replace('/dist/', '/src/'),
        'utf8',
    );
    return raw
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('\n')
        .map((line) => line.replace(/\/\/.*$/, ''))
        .join('\n');
})();

test('the Transcribe path is told whether to separate voices', () => {
    // Read from the same switch the on-demand path passes to its session, so a
    // deployment cannot have separation on for one engine and off for the other.
    assert.match(code, /setDiarization\(\s*switches\.diarizeVirtualParticipant\s*\)/);
});

test('it is applied before either engine is chosen', () => {
    // So the Amazon Transcribe fallback is covered too, not just the branch
    // that was taken first.
    const applied = code.search(/setDiarization\(\s*switches\./);
    const engine = code.indexOf("resolveVpAsrEngine(switches) === 'microvm'");
    assert.ok(applied > -1 && engine > -1, 'both call sites must be present');
    assert.ok(applied < engine);
});

test('Transcribe is still asked for the labels this feature consumes', () => {
    // The request was always there and the labels were thrown away. Removing it
    // now would silently turn every diarized VP meeting back into one speaker,
    // and no behavioural test can see it without a live Transcribe session.
    assert.match(code, /ShowSpeakerLabel:\s*true/);
});
