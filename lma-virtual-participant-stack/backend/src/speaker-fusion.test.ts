/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How a row's speaker is chosen from the page's active-speaker name and the
 * engine's voice label (speaker-fusion.ts).
 *
 * Rows here are given as the seconds the page showed each name during them,
 * which is what the callers pass from the roster timeline. The WebSocket
 * transcriber has a copy of the module and of these tests; keep them in step.
 */
import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

import { DEFAULT_FUSION_THRESHOLDS, SpeakerFusion, isVoiceId } from './speaker-fusion.js';

const SAM = 'Sam Lee';
const ANA = 'Ana Ruiz';

/** A final row whose span the page showed as the given names, the first being the page's name for it. */
const row = (label: string | undefined, shown: Array<[string, number]>, rowId?: string) => ({
    label,
    pageName: shown[0][0],
    coverage: shown,
    rowId,
});

/** Prove `label` to `name` with `seconds` of uncontested speech, in rows of five seconds. */
const prove = (fusion: SpeakerFusion, label: string, name: string, seconds = 30): void => {
    for (let t = 0; t < seconds; t += 5) {
        fusion.attribute(row(label, [[name, 5]]));
    }
};

describe('the name vote', () => {
    it('names rows by the page until the voice has been heard long enough', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM, 15);
        assert.equal(fusion.ownerOf('spk_0'), undefined);
        // A short row the page shows as Ana keeps Ana's name: nothing is proven yet.
        assert.equal(fusion.attribute(row('spk_0', [[ANA, 1]])).name, ANA);
    });

    it("names every row of a proven voice after its name, whatever the page shows on a short row", () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM);
        assert.equal(fusion.ownerOf('spk_0'), SAM);
        // The page still holds Ana from the previous turn, as Zoom's speaker
        // view does for about 2.7 s after a switch.
        const attribution = fusion.attribute(row('spk_0', [[ANA, 1.8], [SAM, 0.4]]));
        assert.deepEqual(attribution, { speaker: `${SAM} (spk_0)`, name: SAM, source: 'voice' });
    });

    it('names a new turn by its voice at once when the voice changes before the page does', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM);
        prove(fusion, 'spk_1', ANA);
        // Ana answers; the page has not switched away from Sam yet.
        assert.equal(fusion.attribute(row('spk_1', [[SAM, 2.5]])).speaker, `${ANA} (spk_1)`);
    });

    it('weighs the vote by time, so one long turn outweighs several short flickers', () => {
        const fusion = new SpeakerFusion();
        fusion.attribute(row('spk_0', [[ANA, 1]]));
        fusion.attribute(row('spk_0', [[ANA, 1]]));
        fusion.attribute(row('spk_0', [[ANA, 1]]));
        fusion.attribute(row('spk_0', [[SAM, 25]]));
        assert.equal(fusion.leadingNameOf('spk_0'), SAM);
        assert.equal(fusion.ownerOf('spk_0'), SAM);
    });

    it('leaves a voice heard as two people unproven, and names its rows by the page', () => {
        const fusion = new SpeakerFusion();
        for (let i = 0; i < 6; i += 1) {
            fusion.attribute(row('spk_0', [[SAM, 3]]));
            fusion.attribute(row('spk_0', [[ANA, 2.5]]));
        }
        assert.equal(fusion.ownerOf('spk_0'), undefined);
        assert.equal(fusion.attribute(row('spk_0', [[ANA, 2]])).name, ANA);
        assert.equal(fusion.attribute(row('spk_0', [[SAM, 2]])).name, SAM);
    });

    it('does not count time the page attributed to nobody', () => {
        const fusion = new SpeakerFusion();
        for (let i = 0; i < 10; i += 1) {
            fusion.attribute({ label: 'spk_0', pageName: 'Unknown', coverage: [] });
        }
        assert.equal(fusion.ownerOf('spk_0'), undefined);
        assert.equal(fusion.leadingNameOf('spk_0'), undefined);
    });

    it('takes no evidence from a partial', () => {
        const fusion = new SpeakerFusion();
        for (let i = 0; i < 10; i += 1) {
            fusion.attribute({ ...row('spk_0', [[SAM, 5]]), isPartial: true });
        }
        assert.equal(fusion.ownerOf('spk_0'), undefined);
    });

    it('gives a re-emitted final the name it was first given, and counts its speech once', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM, 15);
        const first = fusion.attribute(row('spk_0', [[ANA, 2]], 's-41'));
        prove(fusion, 'spk_0', SAM, 10);
        assert.equal(fusion.ownerOf('spk_0'), SAM);
        // The engine corrects the row's text after the label was proven.
        assert.deepEqual(fusion.attribute(row('spk_0', [[ANA, 2]], 's-41')), first);
    });
});

describe('hysteresis', () => {
    it('does not prove a voice whose leading name holds less than the proving share', () => {
        const fusion = new SpeakerFusion();
        // 65%: above the share a proven voice keeps, below the one that proves it.
        fusion.attribute(row('spk_0', [[SAM, 26], [ANA, 14]]));
        assert.equal(fusion.ownerOf('spk_0'), undefined);
    });

    it('keeps a proven name while its share stays above the keeping share', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM, 40);
        // Short rows the page shows as Ana bring Sam's share down to 66%.
        for (let i = 0; i < 10; i += 1) {
            fusion.attribute(row('spk_0', [[ANA, 2]]));
        }
        assert.equal(fusion.ownerOf('spk_0'), SAM);
    });

    it('drops a proven name once its share falls below the keeping share, and keeps it dropped', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM, 30);
        for (let i = 0; i < 12; i += 1) {
            fusion.attribute(row('spk_0', [[ANA, 2]]));
        }
        assert.equal(fusion.ownerOf('spk_0'), undefined);
        prove(fusion, 'spk_0', SAM, 200);
        assert.equal(fusion.ownerOf('spk_0'), undefined, 'a demoted voice is not proven again in this session');
    });
});

describe('a voice the diarizer gives to two people', () => {
    it('names a long row by the page when the page shows someone else for nearly all of it', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM, 120);
        const attribution = fusion.attribute(row('spk_0', [[ANA, 5.8], [SAM, 0.2]]));
        assert.equal(attribution.name, ANA);
        assert.equal(attribution.source, 'page');
        // The voice id stays with Sam, so it is not shown under Ana's name.
        assert.equal(attribution.speaker, ANA);
        assert.equal(fusion.ownerOf('spk_0'), SAM, 'one contradicting row does not demote');
    });

    it('demotes the voice once such rows accumulate, so later short rows are named by the page', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM, 300);
        fusion.attribute(row('spk_0', [[ANA, 4.5]]));
        fusion.attribute(row('spk_0', [[ANA, 4.5]]));
        assert.equal(fusion.ownerOf('spk_0'), undefined);
        assert.equal(fusion.attribute(row('spk_0', [[ANA, 1.2]])).name, ANA);
        assert.equal(fusion.attribute(row('spk_0', [[SAM, 1.2]])).name, SAM);
    });

    it("is not demoted by the page's hold or flicker on short rows", () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM, 60);
        for (let i = 0; i < 20; i += 1) {
            // Shorter than the shortest row that can contradict the voice.
            fusion.attribute(row('spk_0', [[ANA, DEFAULT_FUSION_THRESHOLDS.contradictionMinRowSeconds - 0.3]]));
            prove(fusion, 'spk_0', SAM, 30);
        }
        assert.equal(fusion.ownerOf('spk_0'), SAM);
    });

    it('forgets isolated contradictions spread over a long meeting', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM, 60);
        for (let i = 0; i < 6; i += 1) {
            fusion.attribute(row('spk_0', [[ANA, 5]]));
            prove(fusion, 'spk_0', SAM, 600);
        }
        assert.equal(fusion.ownerOf('spk_0'), SAM);
    });
});

describe('falling back to the page', () => {
    it('passes a row with no voice label through as the page name', () => {
        assert.deepEqual(new SpeakerFusion().attribute(row(undefined, [[SAM, 4]])), {
            speaker: SAM,
            name: SAM,
            source: 'page',
        });
    });

    it('shows a voice id only with the name it has coincided with for the most speech', () => {
        const fusion = new SpeakerFusion();
        assert.equal(fusion.attribute(row('spk_3', [[SAM, 10]])).speaker, 'Sam Lee (spk_3)');
        assert.equal(fusion.attribute(row('spk_3', [[ANA, 2]])).speaker, ANA);
        assert.equal(fusion.attribute(row('spk_3', [[SAM, 1]])).speaker, 'Sam Lee (spk_3)');
    });

    it('gives a tie to the name the voice met first', () => {
        const fusion = new SpeakerFusion();
        assert.equal(fusion.attribute(row('spk_0', [[SAM, 3]])).speaker, 'Sam Lee (spk_0)');
        assert.equal(fusion.attribute(row('spk_0', [[ANA, 3]])).speaker, ANA);
    });

    it('moves the voice id to another name once that name has more of its speech', () => {
        const fusion = new SpeakerFusion();
        assert.equal(fusion.attribute(row('spk_0', [['Priya Patel', 0.8]])).speaker, 'Priya Patel (spk_0)');
        assert.equal(fusion.attribute(row('spk_0', [[ANA, 5]])).speaker, 'Ana Ruiz (spk_0)');
        assert.equal(fusion.attribute(row('spk_0', [['Priya Patel', 1]])).speaker, 'Priya Patel');
    });

    it('keeps the voices behind one name apart, and proves each to that name', () => {
        const fusion = new SpeakerFusion();
        for (let i = 0; i < 6; i += 1) {
            assert.equal(fusion.attribute(row('spk_0', [['Conference Room', 4]])).speaker, 'Conference Room (spk_0)');
            assert.equal(fusion.attribute(row('spk_1', [['Conference Room', 4]])).speaker, 'Conference Room (spk_1)');
        }
        assert.equal(fusion.ownerOf('spk_0'), 'Conference Room');
        assert.equal(fusion.ownerOf('spk_1'), 'Conference Room');
    });

    it("reads Amazon Transcribe's bare voice index as the same voice id", () => {
        const fusion = new SpeakerFusion();
        prove(fusion, '2', SAM);
        assert.equal(fusion.ownerOf('spk_2'), SAM);
        assert.equal(fusion.attribute(row('2', [[ANA, 1]])).speaker, 'Sam Lee (spk_2)');
    });

    it('names a piece by the page even when its voice is proven, keeping the id only with the proven name', () => {
        const fusion = new SpeakerFusion();
        prove(fusion, 'spk_0', SAM);
        assert.deepEqual(fusion.attributeByPage('spk_0', ANA), { speaker: ANA, name: ANA, source: 'page' });
        assert.equal(fusion.attributeByPage('0', SAM).speaker, 'Sam Lee (spk_0)');
        assert.equal(fusion.attributeByPage(undefined, ANA).speaker, ANA);
        // It records nothing: the voice is still proven to Sam.
        assert.equal(fusion.ownerOf('spk_0'), SAM);
    });
});

describe('enrolled names', () => {
    it("stand alone as the speaker, over the page's name", () => {
        const fusion = new SpeakerFusion();
        assert.deepEqual(fusion.attribute(row('Priya Patel', [[SAM, 6]])), {
            speaker: 'Priya Patel',
            name: 'Priya Patel',
            source: 'enrolled',
        });
    });

    it('take no part in any vote', () => {
        const fusion = new SpeakerFusion();
        for (let i = 0; i < 10; i += 1) {
            fusion.attribute(row('Priya Patel', [[SAM, 6]]));
        }
        assert.equal(fusion.leadingNameOf('Priya Patel'), undefined);
    });

    it('are not taken for voice ids', () => {
        assert.equal(isVoiceId('spk_12'), true);
        assert.equal(isVoiceId('Priya Patel'), false);
        assert.equal(isVoiceId('spk_Priya'), false);
        assert.equal(isVoiceId(undefined), false);
    });
});
