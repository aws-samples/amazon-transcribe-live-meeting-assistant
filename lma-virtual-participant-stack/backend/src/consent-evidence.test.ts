/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Consent evidence screenshots (consent-evidence.ts): where they are stored,
 * and the chat / participant list / restore sequence, against a fake page.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import type { Page } from 'playwright-core';
import { captureMeetingPanels, consentEvidenceKey, toolbarPanels } from './consent-evidence.js';

test('evidence keys are under the folder the consent resolver accepts for the meeting', () => {
    const key = consentEvidenceKey('Weekly sync: Q3/plan-2026', Date.parse('2026-11-02T10:01:30.123Z'), 'chat', 'abc123');
    assert.equal(key, 'lma-consent-evidence/Weekly_sync__Q3_plan_2026/2026-11-02T10-01-30-123Z-chat-abc123.jpg');
    assert.ok(key.length <= 256);
});

interface FakeElement {
    visible: boolean;
    onClick?: () => void;
}

function fakePage(elements: Record<string, FakeElement>, events: string[]): Page {
    const page = {
        isClosed: () => false,
        screenshot: async () => {
            events.push('screenshot');
            return Buffer.from('jpeg');
        },
        $: async (selector: string) => {
            const element = elements[selector];
            if (!element) return null;
            return {
                isVisible: async () => element.visible,
                click: async () => {
                    events.push(`click ${selector}`);
                    element.onClick?.();
                },
            };
        },
    };
    return page as unknown as Page;
}

test('the chat is shot, then the participant list is opened, shot, closed, and the chat reopened', async () => {
    const events: string[] = [];
    const list: FakeElement = { visible: false };
    const page = fakePage(
        {
            '.list': list,
            '#open': { visible: true, onClick: () => (list.visible = true) },
            '#close': { visible: true, onClick: () => (list.visible = false) },
        },
        events,
    );
    const shots = await captureMeetingPanels(
        page,
        toolbarPanels(page, {
            listVisible: ['.list'],
            open: ['#missing', '#open'],
            close: ['#close'],
            reopenChat: async () => {
                events.push('reopen chat');
            },
        }),
    );
    assert.deepEqual(
        shots.map((shot) => shot.view),
        ['chat', 'participants'],
    );
    assert.deepEqual(events, ['screenshot', 'click #open', 'screenshot', 'click #close', 'reopen chat']);
});

test('a participant list already open is shot without clicking anything, and left open', async () => {
    const events: string[] = [];
    const page = fakePage({ '.list': { visible: true }, '#close': { visible: true } }, events);
    const shots = await captureMeetingPanels(page, toolbarPanels(page, { listVisible: ['.list'], open: ['#open'], close: ['#close'] }));
    assert.equal(shots.length, 2);
    assert.deepEqual(events, ['screenshot', 'screenshot']);
});

test('where the chat and the list are on screen together, one screenshot is taken', async () => {
    const events: string[] = [];
    const shots = await captureMeetingPanels(fakePage({}, events), { bothVisible: true });
    assert.deepEqual(
        shots.map((shot) => shot.view),
        ['meeting'],
    );
});

test('a list that cannot be opened leaves just the chat, and a failing screenshot is skipped', async () => {
    const events: string[] = [];
    const page = fakePage({}, events);
    const shots = await captureMeetingPanels(page, toolbarPanels(page, { listVisible: ['.list'], open: ['#open'], close: [] }));
    assert.deepEqual(
        shots.map((shot) => shot.view),
        ['chat'],
    );
    const broken = {
        isClosed: () => false,
        screenshot: async () => {
            throw new Error('target closed');
        },
    } as unknown as Page;
    assert.deepEqual(await captureMeetingPanels(broken), []);
});

test('a list button that opens nothing visible gives no participant screenshot, and is closed again', async () => {
    const events: string[] = [];
    const page = fakePage({ '#open': { visible: true }, '#close': { visible: true } }, events);
    const shots = await captureMeetingPanels(page, toolbarPanels(page, { listVisible: ['.list'], open: ['#open'], close: ['#close'] }));
    assert.deepEqual(
        shots.map((shot) => shot.view),
        ['chat'],
    );
    assert.deepEqual(events, ['screenshot', 'click #open', 'click #close']);
});
