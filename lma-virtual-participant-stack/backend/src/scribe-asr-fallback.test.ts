/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A MicroVM released before its session is finished keeps reconnecting against
 * nothing. Asserted at the source level, like scribe-recording-source.test.ts.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';

const src = readFileSync(
    new URL('./scribe.ts', import.meta.url).pathname.replace('/dist/', '/src/'),
    'utf8',
);

function methodBody(name: string): string {
    const start = src.indexOf(name);
    assert.notEqual(start, -1, `${name} not found in scribe.ts`);
    const rest = src.slice(start);
    const end = rest.slice(1).search(/\n {4}(private|public|async|\/\*\*)/);
    return end === -1 ? rest : rest.slice(0, end + 1);
}

test('every MicroVM release in the ASR path is preceded by finishing the session', () => {
    const body = methodBody('private async runMicrovmTranscription');
    const releases = [...body.matchAll(/this\.releaseMicrovm\(session\)/g)].map((m) => m.index ?? -1);
    assert.ok(releases.length >= 2, 'expected the not-ready branch and the normal end to release');
    let from = 0;
    for (const at of releases) {
        const before = body.slice(from, at);
        assert.match(before, /session\.finish\(\)/, `a release at offset ${at} is not preceded by session.finish()`);
        from = at;
    }
});
