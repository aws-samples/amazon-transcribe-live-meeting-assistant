/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The options the server's Fastify/pino logger is built with.
 *
 * Each record is one JSON object per line on stdout, which CloudWatch Logs
 * Insights reads field by field:
 *
 *   {"level":"INFO","ts":"2026-11-02T10:15:00.123Z","component":"websocket-transcriber",
 *    "sessionId":"...","callId":"...","msg":"[TRANSCRIBING]: [<callId>] - ..."}
 *
 * - `level` is the upper-case label, as in Lambda's JSON log format, so the
 *   dashboard's `@message like /ERROR/` query matches every error record.
 * - `ts` is an ISO 8601 UTC timestamp. `pid` and `hostname` are left out: a
 *   task has one process, and its log stream already names the task.
 * - `sessionId` and `callId` come from the running session's log context (see
 *   log-context.ts), so a call site does not repeat them. A record written
 *   outside a connection (start-up, health checks) has neither.
 * - `msg` is the message text as each call site writes it, with its bracket
 *   markers (`[AUTH]`, `[TRANSCRIBING]` …) unchanged.
 *
 * Records are written synchronously to `process.stdout`, from the same thread
 * as the Embedded Metric Format records metrics/emf.ts writes, so a log line
 * and a metric record can never interleave within one CloudWatch event.
 *
 * `LOG_FORMAT=text` keeps the `pino-pretty` line format instead, for reading a
 * local run in a terminal. pino-pretty writes from a worker thread, so in that
 * format the guarantee above does not hold; a local run extracts no metrics.
 */
import { PINO_REDACT_OPTIONS } from './log-redaction';
import { currentLogContext } from './log-context';

export const LOG_COMPONENT = 'websocket-transcriber';

export interface LoggerOptionsInput {
    /** A pino level name: debug, info, warn or error. */
    level: string;
    /** `text` for pino-pretty lines; anything else is JSON. */
    format?: string;
    /** Where JSON records go; stdout unless a test supplies its own. */
    stream?: { write: (chunk: string) => unknown };
}

// pino's own option types are not imported: pino is fastify's dependency, not
// this package's, and Fastify accepts the plain object.
export const buildLoggerOptions = ({ level, format, stream }: LoggerOptionsInput): Record<string, unknown> => {
    const common = {
        level,
        // Backstop for object-style log records only: pino's redact rewrites
        // properties of the logged object and never inspects the rendered
        // message. Every log line in this package is an interpolated string, so
        // each one is redacted at its call site via describeRequest() /
        // stringifyCallMetaData(); this config covers anything logged as an
        // object.
        redact: PINO_REDACT_OPTIONS,
        mixin: () => currentLogContext(),
    };
    if ((format || '').toLowerCase() === 'text') {
        return {
            ...common,
            transport: {
                target: 'pino-pretty',
                options: {
                    ignore: 'pid,hostname',
                    translateTime: 'SYS:HH:MM:ss.l',
                    colorize: false,
                    levelFirst: true,
                },
            },
        };
    }
    return {
        ...common,
        base: { component: LOG_COMPONENT },
        timestamp: () => `,"ts":"${new Date().toISOString()}"`,
        formatters: {
            level: (label: string) => ({ level: label.toUpperCase() }),
        },
        stream: stream ?? process.stdout,
    };
};
