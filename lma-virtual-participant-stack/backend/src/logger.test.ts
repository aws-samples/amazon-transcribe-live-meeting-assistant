/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The record shape, the level threshold, the bound context and the redaction
 * of the Virtual Participant's logger.
 */
import { strict as assert } from 'node:assert';
import { afterEach, test } from 'node:test';
import { format } from 'node:util';
import {
    REDACTED,
    bindLogContext,
    captureLogs,
    clearSecretValuesForTest,
    createLogger,
    getLogContext,
    isSecretKey,
    parseLogLevel,
    redactText,
    registerSecretValue,
    registerSecretValuesFromEnv,
    setLogLevel,
    setLogSink,
    type LogRecord,
} from './logger.js';

function captureLines(): { lines: string[]; restore: () => void } {
    const lines: string[] = [];
    setLogSink((line) => lines.push(line));
    return { lines, restore: () => setLogSink(null) };
}

afterEach(() => {
    clearSecretValuesForTest();
    setLogSink(null);
    setLogLevel(undefined);
    bindLogContext({ vpId: undefined, callId: undefined, meetingPlatform: undefined, extra: undefined });
    delete process.env.LOG_FORMAT;
});

test('a record is one JSON line with ts, level, component and msg', () => {
    const { lines } = captureLines();
    createLogger('unit').info('hello', 42);

    assert.equal(lines.length, 1);
    assert.ok(!lines[0].includes('\n'));
    const record = JSON.parse(lines[0]) as LogRecord;
    assert.equal(record.level, 'INFO');
    assert.equal(record.component, 'unit');
    assert.equal(record.msg, 'hello 42');
    assert.ok(!Number.isNaN(Date.parse(record.ts)));
    assert.deepEqual(Object.keys(record).slice(0, 4), ['ts', 'level', 'component', 'msg']);
});

test('msg is the text console would have printed for the same arguments', () => {
    const { lines } = captureLines();
    const args: unknown[] = ['[zoom] attempt %d of %s:', 2, 'three', { a: [1, 2] }, 'tail'];
    createLogger('unit').info(...args);

    assert.equal((JSON.parse(lines[0]) as LogRecord).msg, format(...args));
});

test('a multi-line message and a stack trace stay in one record', () => {
    const { lines } = captureLines();
    const err = new Error('boom');
    createLogger('unit').error('failed:', err);

    assert.equal(lines.length, 1);
    const record = JSON.parse(lines[0]) as LogRecord;
    assert.equal(record.level, 'ERROR');
    assert.ok(record.msg.startsWith('failed: Error: boom\n'));
    assert.ok(record.msg.includes('logger.test'));
});

test('levels are written in upper case so a /ERROR/ dashboard filter matches', () => {
    const { lines } = captureLines();
    setLogLevel('debug');
    const log = createLogger('unit');
    log.debug('d');
    log.info('i');
    log.warn('w');
    log.error('e');

    assert.deepEqual(
        lines.map((l) => (JSON.parse(l) as LogRecord).level),
        ['DEBUG', 'INFO', 'WARN', 'ERROR'],
    );
    assert.match(lines[3], /ERROR/);
});

test('LOG_LEVEL sets the threshold and defaults to INFO', () => {
    const { lines } = captureLines();
    const log = createLogger('unit');
    const previous = process.env.LOG_LEVEL;
    try {
        delete process.env.LOG_LEVEL;
        log.debug('hidden at the default');
        log.info('shown at the default');

        process.env.LOG_LEVEL = 'WARN';
        log.info('hidden at WARN');
        log.warn('shown at WARN');
        assert.equal(log.isLevelEnabled('info'), false);

        process.env.LOG_LEVEL = 'DEBUG';
        log.debug('shown at DEBUG');
        assert.equal(log.isLevelEnabled('debug'), true);
    } finally {
        if (previous === undefined) delete process.env.LOG_LEVEL;
        else process.env.LOG_LEVEL = previous;
    }
    assert.deepEqual(
        lines.map((l) => (JSON.parse(l) as LogRecord).msg),
        ['shown at the default', 'shown at WARN', 'shown at DEBUG'],
    );
});

test('parseLogLevel accepts the stack parameter values in any case and falls back to info', () => {
    assert.equal(parseLogLevel('DEBUG'), 'debug');
    assert.equal(parseLogLevel('Info'), 'info');
    assert.equal(parseLogLevel('WARNING'), 'warn');
    assert.equal(parseLogLevel('error'), 'error');
    assert.equal(parseLogLevel(undefined), 'info');
    assert.equal(parseLogLevel('verbose'), 'info');
});

test('the bound meeting context is on every record from every module', () => {
    const { lines } = captureLines();
    bindLogContext({ vpId: 'vp-1', meetingPlatform: 'ZOOM' });
    createLogger('a').info('before the call id');
    bindLogContext({ callId: 'call-1' });
    createLogger('b').info('after the call id');

    const [first, second] = lines.map((l) => JSON.parse(l) as LogRecord);
    assert.equal(first.vpId, 'vp-1');
    assert.equal(first.meetingPlatform, 'ZOOM');
    assert.equal(first.callId, undefined);
    assert.equal(second.callId, 'call-1');
    assert.equal(second.component, 'b');
    assert.deepEqual(getLogContext(), { vpId: 'vp-1', meetingPlatform: 'ZOOM', callId: 'call-1' });
});

test('binding an empty value removes the field', () => {
    bindLogContext({ extra: 'x' });
    bindLogContext({ extra: '' });
    assert.equal(getLogContext().extra, undefined);
});

test('a child logger adds its fields and keeps the bound context', () => {
    const { lines } = captureLines();
    bindLogContext({ vpId: 'vp-1' });
    createLogger('nova-agent').child({ sessionId: 's-1' }).info('opened');

    const record = JSON.parse(lines[0]) as LogRecord;
    assert.equal(record.sessionId, 's-1');
    assert.equal(record.vpId, 'vp-1');
    assert.equal(record.component, 'nova-agent');
});

test('methods are bound, so they can be passed as callbacks', async () => {
    const captured = captureLogs();
    try {
        const { error } = createLogger('unit');
        await Promise.reject(new Error('detached')).catch(error);
    } finally {
        captured.restore();
    }
    assert.match(captured.messages()[0], /detached/);
});

test('captureLogs collects records and restore puts the sink back', () => {
    const outer = captureLines();
    const captured = captureLogs();
    createLogger('unit').info('[tool-ack] source=model');
    captured.restore();
    createLogger('unit').info('after');

    assert.deepEqual(captured.messages(), ['[tool-ack] source=model']);
    assert.equal(outer.lines.length, 1);
});

test('LOG_FORMAT=text writes a readable line for a local run', () => {
    const { lines } = captureLines();
    process.env.LOG_FORMAT = 'text';
    createLogger('zoom').warn('slow');

    assert.match(lines[0], /^\S+Z WARN \[zoom\] slow$/);
});

test('credential-named keys in object arguments are redacted at any depth', () => {
    const { lines } = captureLines();
    createLogger('unit').info('config:', {
        meetingId: '123',
        meetingPassword: 'hunter22',
        nested: { auth: { accessToken: 'abc', Authorization: 'Bearer xyz' }, list: [{ client_secret: 's' }] },
    });

    const msg = (JSON.parse(lines[0]) as LogRecord).msg;
    assert.ok(msg.includes("meetingId: '123'"));
    for (const secret of ['hunter22', "'abc'", 'xyz', "'s'"]) {
        assert.ok(!msg.includes(secret), `${secret} leaked: ${msg}`);
    }
    assert.ok(msg.includes(REDACTED));
});

test('redaction does not change the caller object', () => {
    const value = { password: 'p4ssword' };
    createLogger('unit').info(value);
    assert.equal(value.password, 'p4ssword');
});

test('an error keeps its stack and class while its own credential fields are redacted', () => {
    const { lines } = captureLines();
    class SdkError extends Error {}
    const err = Object.assign(new SdkError('denied'), { config: { headers: { Authorization: 'Bearer abcdefghij0123456789' } } });
    createLogger('unit').error(err);

    const msg = (JSON.parse(lines[0]) as LogRecord).msg;
    assert.ok(msg.includes('denied'));
    assert.ok(msg.includes('logger.test'));
    assert.ok(!msg.includes('abcdefghij'));
});

test('bound fields and child fields with credential names are redacted', () => {
    const { lines } = captureLines();
    bindLogContext({ vpId: 'vp-1' });
    createLogger('unit', { apiKey: 'k-123456' }).info('x');

    const record = JSON.parse(lines[0]) as LogRecord;
    assert.equal(record.apiKey, REDACTED);
});

test('JWTs, bearer tokens and secrets in JSON text are redacted from the message', () => {
    const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U';
    const text = redactText(`token ${jwt} header Bearer abc.def-ghi.0123456789xyz {"password":"p@ss","meetingId":"42"}`);

    assert.ok(!text.includes(jwt));
    assert.ok(!text.includes('abc.def-ghi'));
    assert.equal(redactText('Bearer authentication failed'), 'Bearer authentication failed');
    assert.ok(!text.includes('p@ss'));
    assert.ok(text.includes('"meetingId":"42"'));
    assert.ok(text.includes(`Bearer ${REDACTED}`));
});

test('a registered secret value is replaced wherever it appears', () => {
    const { lines } = captureLines();
    registerSecretValue('Pa55-code');
    registerSecretValue('12'); // too short to register
    createLogger('unit').info('page console: typed Pa55-code into the field, attempt 12');

    const msg = (JSON.parse(lines[0]) as LogRecord).msg;
    assert.equal(msg, `page console: typed ${REDACTED} into the field, attempt 12`);
});

test('isSecretKey matches credential names and not counts or names', () => {
    for (const key of ['password', 'meeting_password', 'IdToken', 'x-api-key', 'zoomClientSecret', 'headersecret']) {
        assert.ok(isSecretKey(key), key);
    }
    for (const key of ['tokenCount', 'maxTokens', 'secretName', 'meetingId', 'passwordRequired']) {
        assert.ok(!isSecretKey(key), key);
    }
});

test('a record whose bound field cannot be serialised still writes the message', () => {
    const { lines } = captureLines();
    createLogger('unit', { big: BigInt(1) }).info('still here');

    const record = JSON.parse(lines[0]) as LogRecord;
    assert.equal(record.msg, 'still here');
});

test('bound and child fields cannot replace the record\'s own fields', () => {
    const { lines } = captureLines();
    bindLogContext({ msg: 'bound', level: 'bound' });
    createLogger('unit', { component: 'other', ts: 'then' }).child({ msg: 'child' }).warn('the message');

    const record = JSON.parse(lines[0]) as LogRecord;
    assert.equal(record.msg, 'the message');
    assert.equal(record.level, 'WARN');
    assert.equal(record.component, 'unit');
    assert.notEqual(record.ts, 'then');
});

test('the credential environment variables are registered as secret values', () => {
    const { lines } = captureLines();
    registerSecretValuesFromEnv({ MEETING_PASSWORD: 'env-pass-123', SIMLI_API_KEY: 'simli-key-456' });
    createLogger('unit').info('filled env-pass-123 and sent simli-key-456');

    assert.equal((JSON.parse(lines[0]) as LogRecord).msg, `filled ${REDACTED} and sent ${REDACTED}`);
});

test('always writes an INFO record at any threshold', () => {
    const { lines } = captureLines();
    setLogLevel('error');
    const log = createLogger('main');
    log.info('hidden at ERROR');
    log.always('VP exit: reason=left trigger=chat:leave-command');

    assert.equal(lines.length, 1);
    const record = JSON.parse(lines[0]) as LogRecord;
    assert.equal(record.level, 'INFO');
    assert.equal(record.msg, 'VP exit: reason=left trigger=chat:leave-command');
});
