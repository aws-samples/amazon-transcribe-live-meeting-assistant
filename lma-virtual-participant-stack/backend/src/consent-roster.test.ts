/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Who the consent flow lists as present (consent-roster.ts). Under the
 * each-participant rule the list decides the gate, so it must never be shorter
 * than the meeting: a seat no name accounts for is an unidentified
 * participant, who can never consent.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { ConsentRoster, UNIDENTIFIED_PARTICIPANT, noteParticipantSeen, onParticipantSeen } from './consent-roster.js';

const OWN = ['LMA (Ana Silva)', 'LMA'];

function rosterAt(start = 1000) {
    let now = start;
    const roster = new ConsentRoster(OWN, () => now);
    return { roster, advance: (ms: number) => (now += ms) };
}

test('the VP itself is never listed', () => {
    const { roster } = rosterAt();
    roster.noteSeen('LMA (Ana Silva)');
    roster.noteSeen('lma');
    roster.noteSeen('Bo Chen');
    assert.deepEqual(roster.present(false), ['Bo Chen']);
});

test('seats no name accounts for are unidentified participants', () => {
    const { roster } = rosterAt();
    roster.noteCount(4, false);
    roster.noteSeen('Bo Chen');
    assert.deepEqual(roster.present(true), ['Bo Chen', `${UNIDENTIFIED_PARTICIPANT} 1`, `${UNIDENTIFIED_PARTICIPANT} 2`]);
    assert.deepEqual(roster.present(false), ['Bo Chen'], 'no placeholders outside the each-participant rule');
});

test('names seen before somebody joined stop counting until they are seen again', () => {
    const { roster, advance } = rosterAt();
    roster.noteCount(3, false);
    roster.noteSeen('Bo Chen');
    roster.noteSeen('Cy Diaz');
    assert.deepEqual(roster.present(true), ['Bo Chen', 'Cy Diaz']);
    advance(1000);
    assert.equal(roster.noteCount(4, true), 'joined');
    assert.deepEqual(roster.present(true), [1, 2, 3].map((n) => `${UNIDENTIFIED_PARTICIPANT} ${n}`));
    advance(1000);
    assert.equal(roster.noteSeen('Bo Chen'), true, 'seen again: the list changes');
    assert.deepEqual(roster.present(true), ['Bo Chen', `${UNIDENTIFIED_PARTICIPANT} 1`, `${UNIDENTIFIED_PARTICIPANT} 2`]);
});

test('a departure while consent is pending resets who counts; one while granted does not', () => {
    const pending = rosterAt();
    pending.roster.noteCount(3, false);
    pending.roster.noteSeen('Bo Chen');
    pending.roster.noteSeen('Cy Diaz');
    pending.advance(1000);
    assert.equal(pending.roster.noteCount(2, false), 'left');
    // Either name could be the one who left, so the one seat is unaccounted for.
    assert.deepEqual(pending.roster.present(true), [`${UNIDENTIFIED_PARTICIPANT} 1`]);

    const granted = rosterAt();
    granted.roster.noteCount(3, false);
    granted.roster.noteSeen('Bo Chen');
    granted.roster.noteSeen('Cy Diaz');
    granted.advance(1000);
    assert.equal(granted.roster.noteCount(2, true), 'left');
    // Everybody left was accounted for and consented; nobody new can be among them.
    assert.deepEqual(granted.roster.present(true), ['Bo Chen', 'Cy Diaz']);
});

test('a complete platform list is the list; an incomplete one is topped up with unidentified seats', () => {
    const { roster } = rosterAt();
    roster.noteCount(3, false);
    assert.equal(roster.noteRoster(['Bo Chen', 'LMA (Ana Silva)', 'Cy Diaz']), null);
    assert.equal(roster.rosterIsComplete, true);
    assert.deepEqual(roster.present(true), ['Bo Chen', 'Cy Diaz']);
    assert.equal(roster.noteRoster(['Bo Chen', 'Cy Diaz', 'Dee Evans']), 'joined');
    roster.noteCount(5, false);
    roster.noteRoster(['Bo Chen', 'Cy Diaz', 'Dee Evans']);
    assert.equal(roster.rosterIsComplete, false, 'four seats, three names');
    assert.equal(roster.present(true).length, 4);
    assert.ok(roster.present(true).includes(`${UNIDENTIFIED_PARTICIPANT} 1`));
});

test('with no count and no list, somebody is always unaccounted for under each-participant', () => {
    const { roster } = rosterAt();
    roster.noteSeen('Bo Chen');
    assert.deepEqual(roster.present(true), ['Bo Chen', `${UNIDENTIFIED_PARTICIPANT} 1`]);
    assert.deepEqual(roster.present(false), ['Bo Chen']);
});

test('display names are matched without regard to case or spacing', () => {
    const { roster } = rosterAt();
    roster.noteSeen('Bo  Chen');
    assert.equal(roster.noteSeen('bo chen'), false);
    assert.deepEqual(roster.present(false), ['Bo Chen']);
});

test('speaker sightings reach every listener and a failing listener breaks nothing', () => {
    const seen: string[] = [];
    const stopBad = onParticipantSeen(() => {
        throw new Error('listener failed');
    });
    const stop = onParticipantSeen((name) => seen.push(name));
    noteParticipantSeen('Bo Chen');
    stop();
    stopBad();
    noteParticipantSeen('Cy Diaz');
    assert.deepEqual(seen, ['Bo Chen']);
});

test('a display name that reads as a placeholder is never listed, so it cannot fill an unidentified seat', () => {
    const { roster } = rosterAt();
    roster.noteCount(3, false);
    assert.equal(roster.noteSeen(`${UNIDENTIFIED_PARTICIPANT} 1`), false);
    roster.noteRoster([`${UNIDENTIFIED_PARTICIPANT} 2`]);
    roster.noteSeen('Bo Chen');
    assert.deepEqual(roster.present(true), ['Bo Chen', `${UNIDENTIFIED_PARTICIPANT} 1`]);
});
