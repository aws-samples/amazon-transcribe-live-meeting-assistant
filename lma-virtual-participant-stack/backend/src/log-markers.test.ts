/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The log text that something outside this package searches for.
 *
 * The troubleshooting agent (lma-meetingassist-setup-stack/src/troubleshooting_*.py
 * and its prompt, TroubleshootingAgentSystemPrompt.md) matches VP lines with
 * Logs Insights `@message like "<marker>"`, and the docs tell operators which
 * bracket tags to search for. Those searches keep working on JSON records only
 * while the marker is still written and is still a literal substring of the
 * serialised line. These tests pin both, and pin the one parse expression the
 * agent is taught, on a JSON record and on a plain-text line alike: lines
 * written before the participant wrote JSON records are plain text, and stay
 * in the log group for its retention period.
 *
 * The behaviour behind each marker (that a stop request writes `[stop-request]`,
 * that a tool lookup writes `[tool-ack]` …) is asserted by that module's own
 * tests; this file guards the contract with the readers.
 */
import { strict as assert } from 'node:assert';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import { captureLogs, createLogger, setLogSink } from './logger.js';
import { MetricEmitter } from './metrics-emf.js';
import { NOVA_SONIC_STREAMS_THROTTLED } from './metrics-catalogue.js';

interface Marker {
    /** The text a reader searches for. */
    marker: string;
    /** How the source writes it, where that differs from `marker`. */
    source?: string;
    /** Who relies on it. */
    readers: string;
}

const MARKERS: Marker[] = [
    { marker: 'VP exit:', readers: 'troubleshooting agent; docs/troubleshooting-agent.md' },
    { marker: 'reason=', source: 'reason=${exitInfo.reason}', readers: 'troubleshooting agent (VP exit line)' },
    { marker: 'trigger=', source: "trigger=${exitInfo.trigger ?? 'n/a'}", readers: 'troubleshooting agent (VP exit line)' },
    { marker: 'Received SIGTERM', source: 'Received ${signal}', readers: 'troubleshooting agent' },
    { marker: 'due to external termination', readers: 'troubleshooting agent' },
    { marker: '[terminate] signalling app to exit', readers: 'troubleshooting agent (MicroVM supervisor)' },
    { marker: '[stop-request]', readers: 'docs/voice-assistant.md' },
    { marker: '[tool-ack]', readers: 'docs/nova-sonic-setup.md' },
    { marker: '[translator-turn]', readers: 'docs/voice-assistant.md' },
    { marker: '[nova-throttle]', readers: 'docs/voice-assistant.md' },
    { marker: '[DIARIZATION]', readers: 'docs/virtual-participant.md' },
    { marker: '[SETTINGS]', readers: 'operators checking which settings a participant started with' },
];

const SRC = new URL('../src/', import.meta.url);

function productionSource(dir: URL = SRC): string {
    let text = '';
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
        if (entry.isDirectory()) {
            text += productionSource(new URL(`${entry.name}/`, dir));
        } else if (entry.name.endsWith('.ts') && !entry.name.endsWith('.test.ts') && entry.name !== 'logger.ts') {
            text += readFileSync(new URL(entry.name, dir), 'utf8');
        }
    }
    return text;
}

test('every marker a reader relies on is still written by the VP', () => {
    const source = productionSource();
    for (const { marker, source: written, readers } of MARKERS) {
        assert.ok(source.includes(written ?? marker), `${marker} (read by ${readers}) is no longer in src/`);
    }
});

test('the lifecycle lines are written whatever the LOG_LEVEL threshold', () => {
    // The troubleshooting agent finds how a participant ended from these;
    // a deployment whose LogLevel is WARN or ERROR must still write them.
    const lifecycle = [
        'VP exit: ${',
        'Received ${signal}',
        'status updated to COMPLETED due to external termination',
        'Successfully updated VP ${this.participantId} status to',
    ];
    const lines = productionSource().split('\n');
    for (const marker of lifecycle) {
        const at = lines.findIndex((line) => line.includes(marker));
        assert.ok(at >= 0, `${marker} is no longer in src/`);
        const call = lines.slice(Math.max(0, at - 2), at + 1).join('\n');
        assert.match(call, /\blog\.always\(/, `${marker} is not written with log.always`);
    }
});

test('every marker is a literal substring of the JSON record, so @message like still matches it', () => {
    const lines: string[] = [];
    setLogSink((line) => lines.push(line));
    try {
        for (const { marker } of MARKERS) createLogger('markers').info(`${marker} x=1`);
    } finally {
        setLogSink(null);
    }
    MARKERS.forEach(({ marker }, i) => {
        // JSON escapes only quotes, backslashes and control characters, so a
        // marker free of them is written verbatim.
        assert.ok(!/["\\\u0000-\u001f]/.test(marker), `${marker} would be escaped in JSON`);
        assert.ok(lines[i].includes(marker), `${marker} missing from ${lines[i]}`);
    });
});

test('the trigger= parse the troubleshooting agent is taught reads the plain line and the JSON record alike', () => {
    // TroubleshootingAgentSystemPrompt.md teaches this expression.
    const parse = /trigger=(?<trigger>[^\s"]+)/;
    const lines: string[] = [];
    setLogSink((line) => lines.push(line));
    try {
        createLogger('main').info('VP exit: reason=left trigger=chat:leave-command');
        createLogger('main').info('VP exit: reason=left trigger=chat:leave-command requestedBy="Alice"');
    } finally {
        setLogSink(null);
    }
    const plain = 'VP exit: reason=left trigger=chat:leave-command';
    for (const line of [plain, ...lines]) {
        assert.equal(line.match(parse)?.groups?.trigger, 'chat:leave-command', line);
    }
    // The pattern the prompt replaced runs on through the JSON when trigger=
    // is the last field of the message.
    assert.notEqual(lines[0].match(/trigger=(?<trigger>\S+)/)?.groups?.trigger, 'chat:leave-command');
});

test('an EMF record is written to stdout as the whole line, not inside a log record', () => {
    const written: string[] = [];
    const captured = captureLogs();
    const realWrite = process.stdout.write;
    process.stdout.write = ((chunk: string | Uint8Array) => {
        written.push(String(chunk));
        return true;
    }) as typeof process.stdout.write;
    try {
        new MetricEmitter('LMA/Test').count(NOVA_SONIC_STREAMS_THROTTLED);
    } finally {
        process.stdout.write = realWrite;
        captured.restore();
    }
    assert.equal(captured.records.length, 0, 'nothing went through the logger');
    assert.equal(written.length, 1);
    const record = JSON.parse(written[0]) as Record<string, unknown>;
    assert.ok(record._aws, 'the record is EMF at the top level');
    assert.equal(record.msg, undefined);
});
