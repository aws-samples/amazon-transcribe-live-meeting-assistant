/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Virtual Participant's logger: one JSON object per line on stdout.
 *
 * Every line the container writes reaches CloudWatch Logs unchanged on every
 * launch type, and CloudWatch Logs Insights discovers the fields of a JSON
 * line on its own, so a record written here can be filtered by `level`,
 * `vpId` or `callId` without a `parse` step. A record looks like:
 *
 *   {"ts":"2026-11-02T10:15:00.123Z","level":"INFO","component":"status-manager",
 *    "msg":"Successfully updated VP <id> status to JOINED with preserved fields",
 *    "vpId":"<id>","callId":"...","meetingPlatform":"ZOOM"}
 *
 * - `msg` is the same human-readable text `console.log` would have printed
 *   for the same arguments, so the bracket markers and prefixes that the docs,
 *   the troubleshooting agent and the tests search for (`[stop-request]`,
 *   `VP exit:` …) are still substrings of the line.
 * - `level` is upper case, as in Lambda's JSON log format, so a dashboard
 *   query of `@message like /ERROR/` matches every error record.
 * - The meeting's identity (`vpId`, `callId`, `meetingPlatform`) is bound once
 *   per process with {@link bindLogContext}; a VP container serves exactly one
 *   meeting, so every module's records carry it without passing it around.
 *   {@link Logger.child} adds fields for a narrower scope, such as one voice
 *   assistant session.
 * - `LOG_LEVEL` (DEBUG, INFO, WARN or ERROR; the stack's `LogLevel` parameter)
 *   sets the threshold. Unset or unrecognised means INFO. The participant's
 *   lifecycle lines use {@link Logger.always} and are written at any threshold.
 * - `LOG_FORMAT=text` writes `<ts> <LEVEL> [<component>] <msg>` instead, for
 *   reading a local run in a terminal.
 *
 * Nothing secret is written: object arguments and bound fields have the
 * values of credential-named keys replaced (see {@link isSecretKey}), JWTs and
 * bearer tokens in the message text are replaced, and so is any value passed
 * to {@link registerSecretValue} — the meeting password, for one.
 *
 * Embedded Metric Format records are not written through this module: EMF
 * requires the record to be the whole line, so `metrics-emf.ts` writes its own.
 */
import { formatWithOptions, inspect } from 'node:util';

export type LogLevelName = 'debug' | 'info' | 'warn' | 'error';
export type LogFields = Record<string, unknown>;

export interface LogRecord {
    ts: string;
    level: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';
    component: string;
    msg: string;
    [field: string]: unknown;
}

export interface Logger {
    debug: (...args: unknown[]) => void;
    info: (...args: unknown[]) => void;
    warn: (...args: unknown[]) => void;
    error: (...args: unknown[]) => void;
    /**
     * An INFO record written whatever the threshold. Only for the few
     * lifecycle lines that say how a participant started and ended — the
     * `VP exit:` line, the shutdown-signal line, the status transitions —
     * which the troubleshooting agent and the docs rely on finding even where
     * the stack's `LogLevel` is WARN or ERROR.
     */
    always: (...args: unknown[]) => void;
    /** A logger whose records also carry `fields`, for a narrower scope. */
    child: (fields: LogFields) => Logger;
    isLevelEnabled: (level: LogLevelName) => boolean;
}

export type LogSink = (line: string, record: LogRecord) => void;

export const REDACTED = '[REDACTED]';

const SEVERITY: Record<LogLevelName, number> = { debug: 10, info: 20, warn: 30, error: 40 };
const LABEL: Record<LogLevelName, LogRecord['level']> = {
    debug: 'DEBUG',
    info: 'INFO',
    warn: 'WARN',
    error: 'ERROR',
};

/**
 * The threshold named by a LOG_LEVEL value. Accepts the stack parameter's
 * values in any case, plus the common aliases; anything else is INFO.
 */
export function parseLogLevel(raw: string | undefined): LogLevelName {
    switch ((raw || '').trim().toLowerCase()) {
        case 'debug':
        case 'trace':
            return 'debug';
        case 'warn':
        case 'warning':
            return 'warn';
        case 'error':
        case 'fatal':
        case 'critical':
            return 'error';
        default:
            return 'info';
    }
}

let levelOverride: LogLevelName | null = null;
let cachedRawLevel: string | undefined;
let cachedLevel: LogLevelName = parseLogLevel(process.env.LOG_LEVEL);
cachedRawLevel = process.env.LOG_LEVEL;

function currentLevel(): LogLevelName {
    if (levelOverride) return levelOverride;
    const raw = process.env.LOG_LEVEL;
    if (raw !== cachedRawLevel) {
        cachedRawLevel = raw;
        cachedLevel = parseLogLevel(raw);
    }
    return cachedLevel;
}

/** Override LOG_LEVEL for this process; `undefined` goes back to LOG_LEVEL. */
export function setLogLevel(level: string | undefined): void {
    levelOverride = level === undefined ? null : parseLogLevel(level);
}

// ---------------------------------------------------------------- redaction

const SECRET_KEYS = new Set([
    'password',
    'passcode',
    'pwd',
    'meetingpassword',
    'secret',
    'clientsecret',
    'secretaccesskey',
    'token',
    'accesstoken',
    'idtoken',
    'refreshtoken',
    'sessiontoken',
    'bearertoken',
    'authorization',
    'cookie',
    'setcookie',
    'apikey',
    'xapikey',
    'connectionstring',
    'privatekey',
]);
const SECRET_SUFFIXES = ['password', 'passcode', 'secret', 'token', 'apikey', 'connectionstring', 'privatekey'];

/**
 * Whether a key names a credential. Compared without case, `_` or `-`, so
 * `meeting_password`, `IdToken` and `x-api-key` all match; a key ending in
 * one of the credential words (`zoomClientSecret`) matches too. Counts and
 * names (`tokenCount`, `secretName`) do not.
 */
export function isSecretKey(key: string): boolean {
    const k = key.toLowerCase().replace(/[-_]/g, '');
    return SECRET_KEYS.has(k) || SECRET_SUFFIXES.some((s) => k.endsWith(s));
}

const MAX_REDACT_DEPTH = 6;

function isPassThrough(value: object): boolean {
    return (
        Buffer.isBuffer(value) ||
        ArrayBuffer.isView(value) ||
        value instanceof ArrayBuffer ||
        value instanceof Date ||
        value instanceof RegExp ||
        value instanceof Promise ||
        value instanceof WeakMap ||
        value instanceof WeakSet
    );
}

/**
 * A copy of `value` with the value of every credential-named key replaced by
 * {@link REDACTED}, to the depth `console` prints. A copy keeps its
 * prototype, so `inspect` prints it exactly as it would the original: an
 * Error keeps its stack, a class instance its class name. An object with no
 * own enumerable properties is returned as it is.
 */
export function redact(value: unknown, depth = 0, seen: WeakSet<object> = new WeakSet()): unknown {
    if (value === null || typeof value !== 'object') return value;
    if (depth >= MAX_REDACT_DEPTH || isPassThrough(value)) return value;
    if (seen.has(value)) return '[Circular]';
    seen.add(value);
    try {
        if (Array.isArray(value)) return value.map((v) => redact(v, depth + 1, seen));
        if (value instanceof Map) {
            return new Map(
                [...value.entries()].map(([k, v]) => [
                    k,
                    typeof k === 'string' && isSecretKey(k) ? REDACTED : redact(v, depth + 1, seen),
                ]),
            );
        }
        if (value instanceof Set) return new Set([...value].map((v) => redact(v, depth + 1, seen)));

        const entries = Object.entries(value);
        if (entries.length === 0) return value;
        const copy = Object.create(Object.getPrototypeOf(value)) as Record<string, unknown>;
        if (value instanceof Error) {
            for (const k of ['stack', 'message', 'cause'] as const) {
                if (Object.prototype.hasOwnProperty.call(value, k) && !Object.prototype.propertyIsEnumerable.call(value, k)) {
                    Object.defineProperty(copy, k, {
                        value: k === 'cause' ? redact((value as any)[k], depth + 1, seen) : (value as any)[k],
                        enumerable: false,
                        writable: true,
                        configurable: true,
                    });
                }
            }
        }
        for (const [k, v] of entries) {
            copy[k] = isSecretKey(k) ? REDACTED : redact(v, depth + 1, seen);
        }
        return copy;
    } finally {
        seen.delete(value);
    }
}

const secretValues = new Set<string>();

/** The shortest value {@link registerSecretValue} accepts. */
export const MIN_SECRET_VALUE_LENGTH = 4;

/**
 * Replace every occurrence of `value` in later messages. For credentials the
 * process holds as a plain string (the meeting password), so that no code path
 * — including text forwarded from the meeting page's own console — can write
 * one. Values shorter than {@link MIN_SECRET_VALUE_LENGTH} characters are
 * ignored: replacing "12" everywhere would corrupt every number in the log.
 */
export function registerSecretValue(value: string | undefined | null): void {
    const v = (value || '').trim();
    if (v.length >= MIN_SECRET_VALUE_LENGTH) secretValues.add(v);
}

/**
 * The environment variables through which the VP receives a credential as a
 * plain string. The credentials it fetches later from Secrets Manager — the
 * origin-verify header value (status-manager.ts) and the Zoom sign-in password
 * (zoom-login.ts) — are registered by the code that fetches them.
 */
export const SECRET_ENV_VARS: readonly string[] = [
    'MEETING_PASSWORD',
    'ACS_CONNECTION_STRING',
    'ZOOM_MEETING_SDK_CLIENT_SECRET',
    'ELEVENLABS_API_KEY',
    'SIMLI_API_KEY',
    'USER_ACCESS_TOKEN',
    'USER_ID_TOKEN',
    'USER_REFRESH_TOKEN',
];

/**
 * {@link registerSecretValue} for each of {@link SECRET_ENV_VARS} that is set.
 * Called when this module loads, so it covers every line a module writes, at
 * import time or later; the VP's environment is complete before its process
 * starts on every launch type.
 */
export function registerSecretValuesFromEnv(env: NodeJS.ProcessEnv = process.env): void {
    for (const name of SECRET_ENV_VARS) registerSecretValue(env[name]);
}
registerSecretValuesFromEnv();

const JWT = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g;
const BEARER = /\b(Bearer\s+)[A-Za-z0-9._~+/=-]{20,}/gi;
// `"password":"x"` inside a JSON.stringify()'d object in the message text.
const JSON_SECRET_PAIR = /"([A-Za-z0-9_-]+)"(\s*:\s*)"((?:[^"\\]|\\.)*)"/g;

/** Forget every registered secret value. For tests, which share one process per file. */
export function clearSecretValuesForTest(): void {
    secretValues.clear();
}

/** The message text with tokens and registered secret values replaced. */
export function redactText(text: string): string {
    let out = text.replace(JWT, REDACTED).replace(BEARER, `$1${REDACTED}`);
    out = out.replace(JSON_SECRET_PAIR, (whole, key: string, sep: string) =>
        isSecretKey(key) ? `"${key}"${sep}"${REDACTED}"` : whole,
    );
    for (const secret of secretValues) {
        if (out.includes(secret)) out = out.split(secret).join(REDACTED);
    }
    return out;
}

/** The fields every record has, which bound and child fields cannot replace. */
const RESERVED_FIELDS = new Set(['ts', 'level', 'component', 'msg']);

function redactFields(fields: LogFields): LogFields {
    const out: LogFields = {};
    for (const [k, v] of Object.entries(fields)) {
        if (v === undefined || RESERVED_FIELDS.has(k)) continue;
        out[k] = isSecretKey(k) ? REDACTED : redact(v);
    }
    return out;
}

// ------------------------------------------------------------------ context

const processContext: LogFields = {};

/**
 * Add fields to every record this process writes from now on — the meeting's
 * `vpId`, `callId` and `meetingPlatform`. A field set to `undefined`, `null`
 * or `''` is removed. `ts`, `level`, `component` and `msg` are the record's
 * own and are ignored here.
 */
export function bindLogContext(fields: LogFields): void {
    for (const [k, v] of Object.entries(fields)) {
        if (RESERVED_FIELDS.has(k)) continue;
        if (v === undefined || v === null || v === '') delete processContext[k];
        else processContext[k] = v;
    }
}

/**
 * The spelling `meetingPlatform` is bound in: `MEETING_PLATFORM` arrives as
 * `ZOOM`, `Teams`, `Chime` …, and one spelling lets one filter value match
 * every participant, from the app and the MicroVM supervisor alike.
 */
export function logMeetingPlatform(raw: string | undefined | null): string | undefined {
    const value = (raw || '').trim().toUpperCase();
    return value || undefined;
}

// The meeting's identity is in the environment before the process starts, so
// it is bound here and lines written while modules load carry it too; index.ts
// binds it again with the values it settles on, and adds the call id.
bindLogContext({
    vpId: process.env.VIRTUAL_PARTICIPANT_ID,
    meetingPlatform: logMeetingPlatform(process.env.MEETING_PLATFORM),
});

/** The fields {@link bindLogContext} has bound, as a copy. */
export function getLogContext(): LogFields {
    return { ...processContext };
}

// --------------------------------------------------------------------- sink

const stdoutSink: LogSink = (line) => {
    process.stdout.write(`${line}\n`);
};
let sink: LogSink = stdoutSink;

/** Send records somewhere other than stdout; `null` restores stdout. */
export function setLogSink(next: LogSink | null): void {
    sink = next ?? stdoutSink;
}

export interface CapturedLogs {
    records: LogRecord[];
    /** Each record's `msg`, in order. */
    messages: () => string[];
    restore: () => void;
}

/**
 * Collect records in memory instead of writing them, for tests that assert a
 * marker is logged. `restore()` puts the previous sink back.
 */
export function captureLogs(): CapturedLogs {
    const previous = sink;
    const records: LogRecord[] = [];
    sink = (_line, record) => {
        records.push(record);
    };
    return {
        records,
        messages: () => records.map((r) => r.msg),
        restore: () => {
            sink = previous;
        },
    };
}

// ------------------------------------------------------------------ writing

function formatArgs(args: unknown[]): string {
    const redacted = args.map((a) => (a !== null && typeof a === 'object' ? redact(a) : a));
    return redactText(formatWithOptions({ colors: false }, ...redacted));
}

function serialise(record: LogRecord): string {
    if (process.env.LOG_FORMAT?.toLowerCase() === 'text') {
        return `${record.ts} ${record.level} [${record.component}] ${record.msg}`;
    }
    try {
        return JSON.stringify(record);
    } catch {
        // A bound field that cannot be serialised (a cycle, a BigInt) must not
        // cost the line; keep the message and say what was dropped.
        return JSON.stringify({
            ts: record.ts,
            level: record.level,
            component: record.component,
            msg: record.msg,
            logFieldsError: inspect(Object.keys(record)),
        });
    }
}

function write(level: LogLevelName, component: string, fields: LogFields, args: unknown[], force = false): void {
    if (!force && SEVERITY[level] < SEVERITY[currentLevel()]) return;
    try {
        const record: LogRecord = {
            ts: new Date().toISOString(),
            level: LABEL[level],
            component,
            msg: formatArgs(args),
            ...redactFields(processContext),
            ...fields,
        };
        sink(serialise(record), record);
    } catch (err) {
        // A logger must never be why the meeting fails.
        try {
            process.stderr.write(`logger failed (${String(err)}): ${String(args[0])}\n`);
        } catch {
            /* nothing left to write to */
        }
    }
}

/**
 * A logger for one module. `component` names the module in every record
 * (`nova-agent`, `zoom`); `fields` are added to every record it writes. The
 * methods are bound, so `promise.catch(log.error)` works.
 */
export function createLogger(component: string, fields: LogFields = {}): Logger {
    const own = redactFields(fields);
    return {
        debug: (...args) => write('debug', component, own, args),
        info: (...args) => write('info', component, own, args),
        warn: (...args) => write('warn', component, own, args),
        error: (...args) => write('error', component, own, args),
        always: (...args) => write('info', component, own, args, true),
        child: (more) => createLogger(component, { ...own, ...more }),
        isLevelEnabled: (level) => SEVERITY[level] >= SEVERITY[currentLevel()],
    };
}
