/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The fields that tie a log record to one streaming session.
 *
 * One task serves many WebSocket connections at once, so the session a record
 * belongs to cannot be bound process-wide. Each connection's handlers run
 * inside {@link runWithLogContext} with that connection's own context object,
 * and the logger's `mixin` (see logger-options.ts) adds the current context to
 * every record. AsyncLocalStorage carries the context through everything the
 * handler starts — awaited calls, the transcription loop, timers — so a call
 * site does not pass it along.
 *
 * The context object is mutable on purpose: the call id is only known once
 * the client's START frame has been parsed, after the connection's handlers
 * were registered. {@link bindLogContext} adds it to the context of the
 * session that is running, and every later record of that session carries it.
 */
import { AsyncLocalStorage } from 'node:async_hooks';

export type LogContext = Record<string, string | number | boolean | undefined>;

const storage = new AsyncLocalStorage<LogContext>();

/** Run `fn` with `context` as the session's log context. */
export const runWithLogContext = <T>(context: LogContext, fn: () => T): T => storage.run(context, fn);

/**
 * Add fields to the running session's context. Outside a session (start-up,
 * health checks) there is no context to add to, and nothing happens.
 */
export const bindLogContext = (fields: LogContext): void => {
    const context = storage.getStore();
    if (!context) {
        return;
    }
    for (const [key, value] of Object.entries(fields)) {
        if (value === undefined || value === '') {
            delete context[key];
        } else {
            context[key] = value;
        }
    }
};

/** The running session's fields, for the logger's mixin; `{}` outside one. */
export const currentLogContext = (): LogContext => {
    const context = storage.getStore();
    if (!context) {
        return {};
    }
    const fields: LogContext = {};
    for (const [key, value] of Object.entries(context)) {
        if (value !== undefined) {
            fields[key] = value;
        }
    }
    return fields;
};
