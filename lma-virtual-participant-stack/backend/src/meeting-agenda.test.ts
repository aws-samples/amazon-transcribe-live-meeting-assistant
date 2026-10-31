/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The agenda a VP reads from its own row and puts on the meeting's START event.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { agendaFromVpRow, agendaStartFields, MAX_AGENDA_CHARS } from './meeting-agenda.js';

test('a row without an agenda yields none, so the meeting is created as before', () => {
    assert.equal(agendaFromVpRow(null), null);
    assert.equal(agendaFromVpRow(undefined), null);
    assert.equal(agendaFromVpRow({}), null);
    assert.equal(agendaFromVpRow({ agenda: '   \n ' }), null);
    assert.equal(agendaFromVpRow({ agenda: 42 }), null);
    assert.deepEqual(agendaStartFields(null, 'alice@example.com'), {});
});

test('the stored source is kept for an MCP client and read as the form otherwise', () => {
    assert.deepEqual(agendaFromVpRow({ agenda: 'Budget', agendaSource: 'mcp' }), {
        agenda: 'Budget',
        agendaSource: 'mcp',
    });
    assert.equal(agendaFromVpRow({ agenda: 'Budget', agendaSource: 'scheduled' })?.agendaSource, 'scheduled');
    assert.equal(agendaFromVpRow({ agenda: 'Budget' })?.agendaSource, 'scheduled');
    assert.equal(agendaFromVpRow({ agenda: 'Budget', agendaSource: 'derived' })?.agendaSource, 'scheduled');
});

test('the agenda is trimmed and bounded to what the meeting record accepts', () => {
    assert.equal(agendaFromVpRow({ agenda: '\n Budget\nHiring \n' })?.agenda, 'Budget\nHiring');
    const long = 'x'.repeat(MAX_AGENDA_CHARS + 50);
    assert.equal(agendaFromVpRow({ agenda: long })?.agenda.length, MAX_AGENDA_CHARS);
});

test('the START fields are named as CreateCallInput names them', () => {
    const now = new Date('2026-10-31T10:00:00.000Z');
    assert.deepEqual(agendaStartFields({ agenda: 'Budget', agendaSource: 'mcp' }, 'alice@example.com', now), {
        Agenda: 'Budget',
        AgendaSource: 'mcp',
        AgendaUpdatedBy: 'alice@example.com',
        AgendaUpdatedAt: '2026-10-31T10:00:00.000Z',
    });
    assert.equal(agendaStartFields({ agenda: 'Budget', agendaSource: 'scheduled' }, '', now).AgendaUpdatedBy, 'LMA');
});
