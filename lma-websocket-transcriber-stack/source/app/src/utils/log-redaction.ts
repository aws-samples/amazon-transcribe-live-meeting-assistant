/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Helpers for building connection log lines.
 *
 * The websocket API accepts its bearer token either as an `authorization`
 * header or as an `authorization` / `id_token` / `refresh_token` query string
 * parameter (see `docs/websocket-streaming-api.md`), and the call metadata
 * frame carries the same three values once they have been resolved. The
 * invariant these helpers maintain is that a log line describes *which*
 * request was seen without reproducing anything that could be replayed:
 *
 * - `redactUrl` drops the query string, so the logged path carries no request
 *   parameters at all.
 * - `summarizeHeaders` emits only the fixed set of routing/diagnostic header
 *   names in `LOGGABLE_HEADER_NAMES`, plus a count of everything else. It is an
 *   allowlist rather than a denylist so a header added later is omitted by
 *   default instead of appearing until someone notices.
 * - `redactCallMetaData` returns a copy of the call metadata with the three
 *   token fields replaced by a placeholder.
 *
 * Note on pino `redact`: the server's log calls pass a single interpolated
 * message string, and pino's `redact` option only rewrites properties of a
 * logged *object*. A redact config is therefore configured in index.ts as a
 * backstop for object-style logging, but it cannot reach these message
 * strings — hence these helpers at the call sites.
 */
import { HeaderFields } from './headers';

/**
 * Header names reproduced verbatim in log lines: routing and diagnostic fields
 * that carry no caller-supplied credential. Everything else is counted only.
 */
export const LOGGABLE_HEADER_NAMES: readonly string[] = [
    'host',
    'origin',
    'referer',
    'user-agent',
    'content-type',
    'content-length',
    'connection',
    'upgrade',
    'x-forwarded-for',
    'x-forwarded-proto',
    'x-forwarded-port',
    'sec-websocket-version',
    'sec-websocket-extensions',
];

export const REDACTED = '[REDACTED]';

/**
 * The request path with any query string removed.
 *
 * Fastify's `request.url` is origin-form (path + optional query), so splitting
 * on the first '?' or '#' is sufficient; no host parsing is needed.
 */
export const redactUrl = (url: string | undefined): string => {
    if (!url) {
        return '';
    }
    const cut = url.search(/[?#]/);

    return cut === -1 ? url : url.slice(0, cut);
};

/**
 * A compact, allowlisted rendering of the request headers.
 *
 * Returns something like `{"host":"example","user-agent":"x"} (+3 more)` where
 * the trailing count covers every header whose name is not in
 * LOGGABLE_HEADER_NAMES, so an operator can still see that headers were sent
 * without their values reaching the log.
 */
export const summarizeHeaders = (headers: HeaderFields | undefined): string => {
    if (!headers) {
        return '{}';
    }
    const kept: HeaderFields = {};
    let omitted = 0;
    for (const name of Object.keys(headers)) {
        if (LOGGABLE_HEADER_NAMES.includes(name.toLowerCase())) {
            kept[name] = headers[name];
        } else {
            ++omitted;
        }
    }
    const rendered = JSON.stringify(kept);

    return omitted > 0 ? `${rendered} (+${omitted} more)` : rendered;
};

/**
 * `URI: <path>, Headers: {...}` suffix shared by the connection log lines, so
 * every call site is redacted the same way.
 */
export const describeRequest = (request: {
    url?: string;
    headers?: HeaderFields;
}): string => `URI: <${redactUrl(request.url)}>, Headers: ${summarizeHeaders(request.headers)}`;

/**
 * Replace JWT-shaped runs (three base64url segments separated by dots) with the
 * placeholder.
 *
 * Used on text the server did not compose itself — notably serialized verifier
 * errors, which enumerate every own property of the Error object and so depend
 * on the library's choice of what to attach. Applying this keeps the logged
 * error text limited to the description of the failure.
 */
export const redactTokenLike = (text: string): string =>
    text.replace(/[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, REDACTED);

/** Call metadata fields that hold token material and are never logged. */
export const REDACTED_CALL_METADATA_FIELDS: readonly string[] = [
    'accessToken',
    'idToken',
    'refreshToken',
];

/**
 * A copy of the call metadata with token fields replaced by a placeholder,
 * suitable for `JSON.stringify` in a log line. Fields that are absent stay
 * absent, so the logged shape still reflects what the client actually sent.
 */
export const redactCallMetaData = <T extends object>(callMetaData: T): Record<string, unknown> => {
    const copy: Record<string, unknown> = { ...(callMetaData as Record<string, unknown>) };
    for (const field of REDACTED_CALL_METADATA_FIELDS) {
        if (copy[field] !== undefined) {
            copy[field] = REDACTED;
        }
    }

    return copy;
};

/** `JSON.stringify` of the call metadata with token fields redacted. */
export const stringifyCallMetaData = (callMetaData: object): string =>
    JSON.stringify(redactCallMetaData(callMetaData));

/**
 * pino `redact` paths, applied to object-style log records as a backstop for
 * code that logs a request or call-metadata object rather than a message
 * string built with the helpers above.
 */
export const PINO_REDACT_PATHS: string[] = [
    'req.headers.authorization',
    'req.headers.cookie',
    'req.headers.id_token',
    'req.headers.refresh_token',
    'request.headers.authorization',
    'request.headers.cookie',
    'headers.authorization',
    'headers.cookie',
    'headers.id_token',
    'headers.refresh_token',
    'query.authorization',
    'query.id_token',
    'query.refresh_token',
    'accessToken',
    'idToken',
    'refreshToken',
    'callMetaData.accessToken',
    'callMetaData.idToken',
    'callMetaData.refreshToken',
    'tokens.accessToken',
    'tokens.idToken',
    'tokens.refreshToken',
];
