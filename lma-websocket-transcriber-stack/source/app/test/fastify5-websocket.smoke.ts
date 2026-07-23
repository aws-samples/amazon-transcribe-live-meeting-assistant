/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Smoke test for the Fastify 3->5 + @fastify/websocket 5->11 dependency
 * migration (Dependabot PRs #367 / #369, consolidated in PR #400).
 *
 * The production server (src/index.ts) auto-listens on import and requires a
 * live Cognito user pool, so it can't be imported directly in a unit test.
 * Instead this test reconstructs the two things the major-version bump actually
 * changed, wired exactly as index.ts wires them, and exercises them against the
 * REAL installed fastify@5 / @fastify/websocket@11 / pino-pretty@13 / ws@8:
 *
 *   1. fastify({ logger: { transport: { target: 'pino-pretty', ... } } })
 *      — fastify 5 moved logger config to the transport API; a wrong shape
 *        throws at construction / listen.
 *   2. server.get(path, { websocket: true }, (socket, request) => ...)
 *      — @fastify/websocket v11 passes the WebSocket as the FIRST arg
 *        (v5 passed a { socket } wrapper as `connection`). Calling
 *        socket.on(...) proves the new signature is live.
 *
 * A real ws client connects, sends a binary "audio" frame, and asserts the
 * server echoes it back — end-to-end proof the plugin routes messages under
 * the new API. No AWS is required.
 *
 * Run: npm run smoke
 * Exit 0 = pass, non-zero = fail.
 */
import fastify from 'fastify';
import websocket from '@fastify/websocket';
import WebSocket from 'ws';
import assert from 'assert';

const PORT = parseInt(process.env['SMOKE_PORT'] ?? '38080', 10);
const HOST = '127.0.0.1';

async function main(): Promise<void> {
    // (1) Same logger.transport shape as src/index.ts — must not throw on fastify 5.
    const server = fastify({
        logger: {
            level: 'warn',
            transport: {
                target: 'pino-pretty',
                options: {
                    ignore: 'pid,hostname',
                    translateTime: 'SYS:HH:MM:ss.l',
                    colorize: false,
                    levelFirst: true,
                },
            },
        },
        disableRequestLogging: true,
    });

    // Mirror index.ts's exact wiring so this test guards the real production
    // setup, not an idealized one:
    //   - register(websocket) is NOT awaited
    //   - a global preHandler hook is added
    //   - the websocket route is registered inside server.after()
    // If the route were registered at top level (before the plugin's onRoute
    // hook is wired), fastify 5 / @fastify/websocket 11 would dispatch the
    // upgrade to the normal HTTP handler and the handler's first arg would be a
    // FastifyRequest (no .on) instead of the socket -> "ws.on is not a function"
    // -> HTTP 500 on every connect. The assertions below fail loudly in that case.
    server.register(websocket);

    server.addHook('preHandler', async (request) => {
        if (!request.url.includes('health')) {
            return; // simulate successful auth (index.ts runs jwtVerifier here)
        }
    });

    let serverSawMessage = false;
    let handlerGotSocket = false;

    server.after(() => {
        // (2) @fastify/websocket v11 socket-first handler signature (index.ts).
        server.get('/api/v1/ws', { websocket: true }, (socket, request) => {
            assert.ok(typeof (socket as WebSocket).on === 'function',
                'handler arg 1 must be the WebSocket (v11 signature) — if this is a '
                + 'FastifyRequest, the upgrade fell through to the HTTP handler '
                + '(register/route ordering bug)');
            assert.ok(request && typeof request.url === 'string',
                'handler arg 2 must be the FastifyRequest');
            handlerGotSocket = true;
            socket.on('message', (data: WebSocket.RawData) => {
                serverSawMessage = true;
                socket.send(data); // echo the audio frame back
            });
        });
    });

    // Health route mirrors index.ts:114 — proves plain routes coexist with the WS plugin.
    server.get('/health/check', (_request, reply) => {
        reply.code(200).send({ 'Http-Status': 200, Healthy: true });
    });

    await server.listen({ port: PORT, host: HOST });

    try {
        // HTTP route works under fastify 5.
        const health = await fetch(`http://${HOST}:${PORT}/health/check`);
        assert.strictEqual(health.status, 200, '/health/check should return 200');
        const body = await health.json() as { Healthy?: boolean };
        assert.strictEqual(body.Healthy, true, '/health/check body.Healthy should be true');

        // WebSocket round-trip through @fastify/websocket v11.
        await new Promise<void>((resolve, reject) => {
            const ws = new WebSocket(`ws://${HOST}:${PORT}/api/v1/ws`);
            const timer = setTimeout(() => reject(new Error('WS round-trip timed out (10s)')), 10_000);
            const payload = Buffer.from([0x52, 0x49, 0x46, 0x46, 0x00, 0x01, 0x02, 0x03]); // fake stereo PCM frame

            ws.on('open', () => ws.send(payload));
            ws.on('message', (echo: WebSocket.RawData) => {
                clearTimeout(timer);
                assert.ok(Buffer.isBuffer(echo) || echo instanceof ArrayBuffer || Array.isArray(echo),
                    'echo should be binary');
                assert.ok(Buffer.from(echo as Buffer).equals(payload),
                    'server should echo the exact audio frame back');
                ws.close();
                resolve();
            });
            ws.on('error', reject);
        });

        assert.ok(handlerGotSocket, 'websocket route handler must have received the socket (upgrade recognized)');
        assert.ok(serverSawMessage, 'server-side message handler (v11 signature) must have fired');
        console.log('PASS: fastify 5 booted with pino-pretty transport, health route 200, and @fastify/websocket v11 echoed the audio frame.');
    } finally {
        await server.close();
    }
}

main().then(
    () => process.exit(0),
    (err) => {
        console.error('FAIL:', err instanceof Error ? err.message : err);
        process.exit(1);
    },
);
