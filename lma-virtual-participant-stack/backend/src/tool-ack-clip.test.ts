/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Rendering the lookup acknowledgement clip: what is kept from a Nova Sonic
 * response, and what is refused.
 *
 * The response stream is a stand-in. What is NOT covered here: the live
 * service, which was exercised once by hand against Amazon Bedrock and
 * returned a 2.4-second clip of the phrase in about 1.3 seconds.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { renderToolAckClip, TOOL_ACK_MAX_CLIP_MS, pcmDurationMs } from './tool-ack-clip.js';

const encoder = new TextEncoder();

function event(body: unknown): { chunk: { bytes: Uint8Array } } {
    return { chunk: { bytes: encoder.encode(JSON.stringify({ event: body })) } };
}

/** A client whose single response is `events`, after draining the request. */
function client(events: unknown[], fail?: Error): any {
    return {
        send: async (command: any) => {
            if (fail) throw fail;
            // Consume the opening of the request the way the service would.
            const it = command.input.body[Symbol.asyncIterator]();
            for (let i = 0; i < 3; i++) await it.next();
            return {
                body: (async function* () {
                    for (const e of events) yield e;
                })(),
            };
        },
    };
}

function audio(ms: number): unknown {
    return event({ audioOutput: { content: Buffer.alloc(ms * 32, 3).toString('base64') } });
}

test('the first assistant audio block is the clip', async () => {
    const clip = await renderToolAckClip(client([
        event({ contentStart: { type: 'TEXT', role: 'ASSISTANT' } }),
        event({ textOutput: { role: 'ASSISTANT', content: 'One moment, let me look that up.' } }),
        event({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
        audio(1000),
        audio(1200),
        event({ contentEnd: { type: 'AUDIO' } }),
        event({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
        audio(3000),
    ]), 'amazon.nova-2-sonic-v1:0', 'tiffany');
    assert.ok(clip);
    assert.equal(pcmDurationMs(clip), 2200, 'anything after the first block is not part of it');
});

test('a clip longer than the cap is refused rather than played', async () => {
    const clip = await renderToolAckClip(client([
        event({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
        audio(TOOL_ACK_MAX_CLIP_MS + 500),
        event({ contentEnd: { type: 'AUDIO' } }),
    ]), 'amazon.nova-2-sonic-v1:0', 'tiffany');
    assert.equal(clip, null);
});

test('a failed render yields no clip and does not throw', async () => {
    const realWarn = console.warn;
    console.warn = () => {};
    try {
        const clip = await renderToolAckClip(client([], new Error('AccessDenied')), 'm', 'tiffany');
        assert.equal(clip, null);
    } finally {
        console.warn = realWarn;
    }
});
