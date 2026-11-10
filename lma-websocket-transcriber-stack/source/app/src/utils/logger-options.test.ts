/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The record the server's logger writes, the session context each record
 * carries, and the markers that readers outside this package search for.
 *
 * Records are produced through a real Fastify logger built from
 * buildLoggerOptions, which is how the options reach pino in index.ts.
 */
import { strict as assert } from 'node:assert';
import { EventEmitter } from 'node:events';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import fastify, { FastifyInstance } from 'fastify';
import { LOG_COMPONENT, buildLoggerOptions } from './logger-options';
import { bindLogContext, currentLogContext, runWithLogContext } from './log-context';
import { REDACTED } from './log-redaction';

interface LogLine {
    level?: string;
    ts?: string;
    component?: string;
    msg?: string;
    sessionId?: string;
    callId?: string;
    pid?: unknown;
    hostname?: unknown;
    time?: unknown;
    callMetaData?: { callId?: string; accessToken?: string };
}

const appWithCapture = (level = 'debug'): { app: FastifyInstance; records: () => LogLine[] } => {
    const lines: string[] = [];
    const app = fastify({
        logger: buildLoggerOptions({
            level,
            stream: {
                write: (chunk: string) => {
                    lines.push(chunk);
                },
            },
        }),
    });
    return {
        app,
        records: () => lines.map((l) => JSON.parse(l) as LogLine),
    };
};

test('a record is one JSON line with level, ts, component and msg', async () => {
    const { app, records } = appWithCapture();
    app.log.info('[TRANSCRIBING]: [call-1] - session started');
    await app.close();

    const [record] = records();
    assert.equal(record.level, 'INFO');
    assert.equal(record.component, LOG_COMPONENT);
    assert.equal(record.msg, '[TRANSCRIBING]: [call-1] - session started');
    assert.ok(!Number.isNaN(Date.parse(String(record.ts))));
    assert.equal(record.pid, undefined);
    assert.equal(record.hostname, undefined);
    assert.equal(record.time, undefined);
});

test('levels are upper case so the dashboard\'s /ERROR/ filter matches', async () => {
    const { app, records } = appWithCapture();
    app.log.debug('d');
    app.log.info('i');
    app.log.warn('w');
    app.log.error('e');
    await app.close();

    assert.deepEqual(
        records().map((r) => r.level),
        ['DEBUG', 'INFO', 'WARN', 'ERROR']
    );
});

test('the level threshold is honoured', async () => {
    const { app, records } = appWithCapture('warn');
    app.log.info('hidden');
    app.log.warn('shown');
    await app.close();

    assert.deepEqual(
        records().map((r) => r.msg),
        ['shown']
    );
});

test('a record written inside a session carries its session id, and its call id once bound', async () => {
    const { app, records } = appWithCapture();
    app.log.info('outside any session');
    await runWithLogContext({ sessionId: 's-1' }, async () => {
        app.log.info('connected');
        bindLogContext({ callId: 'call-1' });
        await new Promise((resolve) => setTimeout(resolve, 1));
        app.log.info('after START');
    });
    await app.close();

    const [outside, connected, started] = records();
    assert.equal(outside.sessionId, undefined);
    assert.equal(connected.sessionId, 's-1');
    assert.equal(connected.callId, undefined);
    assert.equal(started.sessionId, 's-1');
    assert.equal(started.callId, 'call-1');
});

test('concurrent sessions keep their own context', async () => {
    const { app, records } = appWithCapture();
    const session = (id: string, delays: number[]): Promise<void> =>
        runWithLogContext({ sessionId: id }, async () => {
            bindLogContext({ callId: `call-${id}` });
            for (const ms of delays) {
                await new Promise((resolve) => setTimeout(resolve, ms));
                app.log.info(`tick ${id}`);
            }
        });
    await Promise.all([session('a', [3, 1, 3]), session('b', [1, 3, 1])]);
    await app.close();

    for (const record of records()) {
        const id = String(record.msg).split(' ')[1];
        assert.equal(record.sessionId, id);
        assert.equal(record.callId, `call-${id}`);
    }
});

test('a listener entered with the connection context logs with it, whoever emits the event', async () => {
    // registerHandlers in index.ts wraps each WebSocket listener this way: an
    // event runs in the context of the code that emitted it, which for a
    // socket is not the connection's handler.
    const { app, records } = appWithCapture();
    const socket = new EventEmitter();
    const context = { sessionId: 's-9' };
    runWithLogContext(context, () => {
        socket.on('message', () => runWithLogContext(context, () => app.log.info('message')));
    });
    socket.emit('message');
    await app.close();

    assert.equal(records()[0].sessionId, 's-9');
});

test('bindLogContext outside a session changes nothing', () => {
    bindLogContext({ callId: 'stray' });
    assert.deepEqual(currentLogContext(), {});
});

test('the redact backstop still applies to object-style records', async () => {
    const { app, records } = appWithCapture();
    app.log.info({ callMetaData: { callId: 'call-1', accessToken: 'eyJsecret' } }, 'object-style record');
    await app.close();

    const record = records()[0];
    assert.equal(record.callMetaData?.callId, 'call-1');
    assert.equal(record.callMetaData?.accessToken, REDACTED);
});

test('LOG_FORMAT=text keeps the pino-pretty line format', () => {
    const options = buildLoggerOptions({ level: 'info', format: 'text' }) as { transport?: { target: string } };
    assert.equal(options.transport?.target, 'pino-pretty');
});

// The troubleshooting agent (lma-meetingassist-setup-stack and its prompt,
// TroubleshootingAgentSystemPrompt.md) and the docs search the transcriber's
// log group for these with `@message like "<marker>"`.
const MARKERS = ['[AUTH]', '[TRANSCRIBING]', '[ASR]', '[DIARIZATION]'];

const productionSource = (dir: string): string => {
    let text = '';
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            text += productionSource(full);
        } else if (entry.name.endsWith('.ts') && !entry.name.endsWith('.test.ts')) {
            text += readFileSync(full, 'utf8');
        }
    }
    return text;
};

test('every marker a reader relies on is still written, verbatim inside the JSON record', async () => {
    // The source scan is the guard: it fails when a marker is renamed. The
    // logging half only shows that a marker survives JSON encoding unescaped.
    // dist/utils/ at run time; the sources are beside dist/.
    const source = productionSource(path.resolve(__dirname, '../../src'));
    const { app, records } = appWithCapture();
    const lines: string[] = [];
    for (const marker of MARKERS) {
        assert.ok(source.includes(marker), `${marker} is no longer in src/`);
        app.log.info(`${marker}: [call-1] - x`);
    }
    await app.close();
    for (const record of records()) {
        lines.push(JSON.stringify(record));
    }
    MARKERS.forEach((marker, i) => {
        assert.ok(lines[i].includes(marker), `${marker} missing from ${lines[i]}`);
    });
});
