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
 *   default instead of appearing until someone notices. The two header values
 *   that are themselves URLs go through `redactUrl` as well, so the same
 *   "no query string" rule holds wherever a URL is logged.
 * - `redactCallMetaData` returns a copy with the token fields replaced by a
 *   placeholder. It covers both spellings used in this package: the camelCase
 *   `accessToken` / `idToken` / `refreshToken` of `CallMetaData`, and the
 *   PascalCase `AccessToken` / `IdToken` / `RefreshToken` of the Kinesis
 *   records built in `calleventdata/transcribe.ts` — so one serializer works
 *   for call metadata, call events and transcript-segment records alike.
 *
 * Note on pino `redact`: the server's log calls pass a single interpolated
 * message string, and pino's `redact` option only rewrites properties of a
 * logged *object* — it never inspects the rendered `msg`. `PINO_REDACT_OPTIONS`
 * is therefore configured in index.ts as a backstop for object-style logging,
 * but it cannot reach these message strings, which is why the helpers are
 * applied at the call sites too.
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

/**
 * Allowlisted headers whose value is a URL, and so may itself carry a query
 * string. `origin` never has a path and `referer` usually does; both are passed
 * through `redactUrl` so the logged value is scheme/host/path only, regardless
 * of what the client sent.
 */
export const URL_VALUED_HEADER_NAMES: readonly string[] = ['origin', 'referer'];

export const REDACTED = '[REDACTED]';

/**
 * The request path with any query string removed.
 *
 * Fastify's `request.url` is origin-form (path + optional query), so splitting
 * on the first '?' or '#' is sufficient; no host parsing is needed. An absolute
 * URL (as a `referer` / `origin` header value is) splits the same way.
 */
export const redactUrl = (url: string | string[] | undefined): string => {
    if (!url) {
        return '';
    }
    // String() rather than a bare cast: the typed call sites always pass a
    // string, but a header value arrives as `string | string[]` from Node and a
    // log line must never be the thing that throws.
    const text = String(url);
    const cut = text.search(/[?#]/);

    return cut === -1 ? text : text.slice(0, cut);
};

/**
 * A compact, allowlisted rendering of the request headers.
 *
 * Returns something like `{"host":"example","user-agent":"x"} (+3 more)` where
 * the trailing count covers every header whose name is not in
 * LOGGABLE_HEADER_NAMES, so an operator can still see that headers were sent
 * without their values reaching the log. A header named in
 * URL_VALUED_HEADER_NAMES has its value passed through `redactUrl` first.
 */
export const summarizeHeaders = (headers: HeaderFields | undefined): string => {
    if (!headers) {
        return '{}';
    }
    const kept: HeaderFields = {};
    let omitted = 0;
    for (const name of Object.keys(headers)) {
        const lowered = name.toLowerCase();
        if (LOGGABLE_HEADER_NAMES.includes(lowered)) {
            kept[name] = URL_VALUED_HEADER_NAMES.includes(lowered)
                ? redactUrl(headers[name])
                : headers[name];
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

/**
 * Field names that hold token material and are never logged.
 *
 * Both spellings are listed because this package uses both: `CallMetaData` (the
 * parsed control frame) is camelCase, while the Kinesis record types built on
 * `CallEventBase` in `calleventdata/eventtypes.ts` are PascalCase. Redaction keys
 * on the field name rather than on the record type, so it applies to every
 * `CallEventBase` variant — including any added later — without this list needing
 * to enumerate them.
 */
export const REDACTED_CALL_METADATA_FIELDS: readonly string[] = [
    'accessToken',
    'idToken',
    'refreshToken',
    'AccessToken',
    'IdToken',
    'RefreshToken',
];

/**
 * A copy of the object with token fields replaced by a placeholder, suitable for
 * `JSON.stringify` in a log line. Fields that are absent stay absent, so the
 * logged shape still reflects what was actually present.
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

/**
 * `JSON.stringify` with the token fields redacted. Use this in place of
 * `JSON.stringify` for any call metadata, call event or KDS record that is
 * interpolated into a log line.
 */
export const stringifyCallMetaData = (callMetaData: object): string =>
    JSON.stringify(redactCallMetaData(callMetaData));

/**
 * pino `redact` paths, applied to object-style log records as a backstop for
 * code that logs a request, call-metadata or KDS-record object rather than a
 * message string built with the helpers above.
 *
 * Both field spellings are covered (see REDACTED_CALL_METADATA_FIELDS), under
 * the object names this package actually uses as log-record keys.
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
    'AccessToken',
    'IdToken',
    'RefreshToken',
    'callMetaData.accessToken',
    'callMetaData.idToken',
    'callMetaData.refreshToken',
    'callMetaData.AccessToken',
    'callMetaData.IdToken',
    'callMetaData.RefreshToken',
    'callMetadata.accessToken',
    'callMetadata.idToken',
    'callMetadata.refreshToken',
    'callEvent.AccessToken',
    'callEvent.IdToken',
    'callEvent.RefreshToken',
    'callEvent.accessToken',
    'callEvent.idToken',
    'callEvent.refreshToken',
    'kdsObject.AccessToken',
    'kdsObject.IdToken',
    'kdsObject.RefreshToken',
    'tokens.accessToken',
    'tokens.idToken',
    'tokens.refreshToken',
];

/**
 * The `redact` option for the Fastify/pino logger, exported as one object so the
 * configuration in index.ts and the test that exercises it cannot drift apart.
 */
export const PINO_REDACT_OPTIONS = {
    paths: PINO_REDACT_PATHS,
    censor: REDACTED,
};
