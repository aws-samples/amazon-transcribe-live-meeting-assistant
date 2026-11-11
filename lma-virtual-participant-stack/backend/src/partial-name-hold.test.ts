/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

import { PartialNameHold } from './partial-name-hold.js';
import type { Attribution } from './speaker-fusion.js';

const page = (name: string): Attribution => ({ speaker: name, name, source: 'page' });
const voice = (name: string, label = 'spk_0'): Attribution => ({
    speaker: `${name} (${label})`,
    name,
    source: 'voice',
});

describe('PartialNameHold', () => {
    it('shows the first name a row is given', () => {
        const hold = new PartialNameHold();
        assert.deepEqual(hold.hold('r1', page('Sam')), page('Sam'));
    });

    it('replaces a page name with a proven voice name on the next partial', () => {
        const hold = new PartialNameHold();
        hold.hold('r1', page('Sam'));
        assert.deepEqual(hold.hold('r1', voice('Ana')), voice('Ana'));
    });

    it('needs two partials in a row before a voice-named row goes back to a page name', () => {
        const hold = new PartialNameHold();
        hold.hold('r1', voice('Ana'));
        assert.equal(hold.hold('r1', page('Sam')).name, 'Ana');
        assert.equal(hold.hold('r1', page('Sam')).name, 'Sam');
    });

    it('does not flip-flop on single readings that alternate', () => {
        const hold = new PartialNameHold();
        const shown = [voice('Ana'), voice('Sam', 'spk_1'), voice('Ana'), voice('Sam', 'spk_1'), voice('Ana')].map(
            (candidate) => hold.hold('r1', candidate).name,
        );
        assert.deepEqual(shown, ['Ana', 'Ana', 'Ana', 'Ana', 'Ana']);
    });

    it('follows a voice change once it is confirmed, and only once', () => {
        const hold = new PartialNameHold();
        const shown = [voice('Sam', 'spk_1'), voice('Sam', 'spk_1'), voice('Ana'), voice('Ana'), voice('Ana')].map(
            (candidate) => hold.hold('r1', candidate).name,
        );
        assert.deepEqual(shown, ['Sam', 'Sam', 'Sam', 'Ana', 'Ana']);
    });

    it('keeps the voice id once the same person is named by the voice', () => {
        const hold = new PartialNameHold();
        hold.hold('r1', voice('Ana'));
        assert.deepEqual(hold.hold('r1', page('Ana')), voice('Ana'));
    });

    it('holds each row separately and forgets a row at its final', () => {
        const hold = new PartialNameHold();
        hold.hold('r1', voice('Ana'));
        hold.hold('r2', page('Sam'));
        assert.deepEqual(hold.close('r1'), voice('Ana'));
        assert.equal(hold.close('r1'), undefined);
        assert.deepEqual(hold.hold('r1', page('Sam')), page('Sam'));
        assert.deepEqual(hold.close('r2'), page('Sam'));
    });
});
