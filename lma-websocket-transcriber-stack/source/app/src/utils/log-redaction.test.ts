/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the connection-log redaction helpers.
 *
 * The websocket API accepts its bearer token in a header OR in the query string
 * (`authorization`, `id_token`, `refresh_token` — see
 * docs/websocket-streaming-api.md), and the call metadata frame carries the same
 * three values. The invariant pinned here is that the text these helpers produce
 * identifies the request but reproduces none of that material, so a log line is
 * not a second copy of the credential.
 *
 * Two end-to-end assertions back the unit tests:
 *
 * - `the log line jwtVerifier emits ...` drives the real verifier with a stub
 *   logger and searches the captured message strings. It covers the call-site
 *   helpers, which is where the message strings are built.
 * - `the pino redact configuration censors ...` builds a Fastify logger from the
 *   exact `PINO_REDACT_OPTIONS` object index.ts installs and writes through real
 *   pino, so the object-style backstop is covered too.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import fastify from 'fastify';
import {
    LOGGABLE_HEADER_NAMES,
    PINO_REDACT_OPTIONS,
    REDACTED,
    describeRequest,
    redactCallMetaData,
    redactTokenLike,
    redactUrl,
    stringifyCallMetaData,
    summarizeHeaders,
} from './log-redaction';

/**
 * A recognisable, JWT-shaped dummy value. Not a real token; the point is that it
 * is a single distinctive substring that must not survive into any log text.
 */
const DUMMY_TOKEN =
    'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkdW1teS1jYW5hcnktdmFsdWUifQ.c2lnbmF0dXJlLWNhbmFyeS12YWx1ZQ';
const DUMMY_REFRESH = 'refreshcanary0000.refreshcanary1111.refreshcanary2222';

/**
 * Shortest run of a dummy token that counts as a leak.
 *
 * Checking only for the whole value would pass on a truncated copy, and a
 * truncated token is still material that should not be in a log. 12 characters
 * of base64url is ~72 bits of the value and is far too distinctive to occur by
 * accident in surrounding log text.
 */
const MIN_LEAKED_RUN = 12;

/**
 * Assert that no contiguous run of `MIN_LEAKED_RUN` or more characters of
 * `secret` appears anywhere in `text`. Every window of that length is checked,
 * so a leak of any prefix, suffix or middle slice fails.
 */
const assertNoTokenRun = (text: string, secret: string, label: string): void => {
    for (let i = 0; i + MIN_LEAKED_RUN <= secret.length; ++i) {
        const window = secret.slice(i, i + MIN_LEAKED_RUN);
        assert.ok(
            !text.includes(window),
            `${label}: a ${MIN_LEAKED_RUN}-character run of the dummy value starting at offset ${i} survived into: ${text}`,
        );
    }
};

test('assertNoTokenRun itself catches a partial leak', () => {
    // Guards the guard: a truncated copy of the token must be reported, or the
    // assertions below would pass vacuously.
    for (const leak of [
        DUMMY_TOKEN.slice(0, 12),
        DUMMY_TOKEN.slice(0, 40),
        DUMMY_TOKEN.slice(30, 50),
        DUMMY_TOKEN.slice(-20),
    ]) {
        assert.throws(() => assertNoTokenRun(`log line ${leak} end`, DUMMY_TOKEN, 'self-test'));
    }
    // And a value that shares nothing with the token does not trip it.
    assertNoTokenRun('URI: </api/v1/ws>, Headers: {"host":"h"}', DUMMY_TOKEN, 'self-test');
});

test('the logged path drops the query string', () => {
    assert.equal(
        redactUrl(`/api/v1/ws?authorization=Bearer%20${DUMMY_TOKEN}&id_token=${DUMMY_TOKEN}`),
        '/api/v1/ws',
    );
    assert.equal(redactUrl('/api/v1/ws'), '/api/v1/ws');
    assert.equal(redactUrl('/health/check#frag'), '/health/check');
    assert.equal(redactUrl(undefined), '');
});

test('only allowlisted header names are rendered, the rest are counted', () => {
    const summary = summarizeHeaders({
        host: 'lma.example.com',
        'user-agent': 'test-agent',
        authorization: `Bearer ${DUMMY_TOKEN}`,
        id_token: DUMMY_TOKEN,
        refresh_token: DUMMY_REFRESH,
        cookie: 'session=abc',
    });

    // Exact equality rather than substring checks: it pins the two allowlisted
    // values, the count of the four that were dropped, and the absence of
    // everything else in one assertion.
    assert.equal(summary, '{"host":"lma.example.com","user-agent":"test-agent"} (+4 more)');
    assertNoTokenRun(summary, DUMMY_TOKEN, 'summarizeHeaders');
    assertNoTokenRun(summary, DUMMY_REFRESH, 'summarizeHeaders');
    assert.ok(!summary.includes('session=abc'));
});

test('a header name is matched case-insensitively against the allowlist', () => {
    // Node lowercases incoming header names, but the helper is also used on plain
    // objects in tests and smoke scripts.
    assert.equal(summarizeHeaders({ Host: 'lma.example.com' }), '{"Host":"lma.example.com"}');
});

test('a URL-valued header keeps its path but loses its query string', () => {
    // A Referer is a full URL, and this API accepts the credential as a query
    // parameter, so an allowlisted URL value has to obey the same rule as the
    // request path itself.
    const summary = summarizeHeaders({
        referer: `https://lma.example.com/index.html?id_token=${DUMMY_TOKEN}`,
        origin: 'https://lma.example.com',
    });

    assert.equal(
        summary,
        '{"referer":"https://lma.example.com/index.html","origin":"https://lma.example.com"}',
    );
    assertNoTokenRun(summary, DUMMY_TOKEN, 'referer');
});

test('the allowlist contains no credential-bearing header name', () => {
    for (const name of ['authorization', 'cookie', 'id_token', 'refresh_token', 'x-api-key']) {
        assert.ok(!LOGGABLE_HEADER_NAMES.includes(name), `${name} must not be logged verbatim`);
    }
});

test('an absent header set renders as an empty object', () => {
    assert.equal(summarizeHeaders(undefined), '{}');
    assert.equal(summarizeHeaders({}), '{}');
});

test('describeRequest reproduces neither the query string nor the credential headers', () => {
    const description = describeRequest({
        url: `/api/v1/ws?authorization=Bearer%20${DUMMY_TOKEN}`,
        headers: { host: 'lma.example.com', authorization: `Bearer ${DUMMY_TOKEN}` },
    });

    assert.ok(description.startsWith('URI: </api/v1/ws>'));
    assertNoTokenRun(description, DUMMY_TOKEN, 'describeRequest');
});

test('JWT-shaped runs in free text are replaced', () => {
    assert.equal(redactTokenLike(`token was ${DUMMY_TOKEN} here`), `token was ${REDACTED} here`);
    // Text with no token-shaped run is returned unchanged.
    assert.equal(redactTokenLike('JwtExpiredError: token expired at 1700000000'),
        'JwtExpiredError: token expired at 1700000000');
});

test('call metadata is logged with its token fields replaced', () => {
    const rendered = stringifyCallMetaData({
        callId: 'call-1',
        callEvent: 'START',
        accessToken: DUMMY_TOKEN,
        idToken: DUMMY_TOKEN,
        refreshToken: DUMMY_REFRESH,
    });

    assert.ok(rendered.includes('call-1'));
    assertNoTokenRun(rendered, DUMMY_TOKEN, 'stringifyCallMetaData');
    assertNoTokenRun(rendered, DUMMY_REFRESH, 'stringifyCallMetaData');
    assert.equal(JSON.parse(rendered).accessToken, REDACTED);
});

test('a PascalCase KDS record has its token fields replaced by the same serializer', () => {
    // writeCallEvent / writeSegmentToKds build AccessToken / IdToken /
    // RefreshToken, not the camelCase names CallMetaData uses. One serializer has
    // to cover both spellings or the KDS log lines stay unredacted.
    const rendered = stringifyCallMetaData({
        EventType: 'ADD_TRANSCRIPT_SEGMENT',
        CallId: 'call-1',
        Transcript: 'hello',
        AccessToken: DUMMY_TOKEN,
        IdToken: DUMMY_TOKEN,
        RefreshToken: DUMMY_REFRESH,
    });

    const parsed = JSON.parse(rendered);
    assert.equal(parsed.CallId, 'call-1');
    assert.equal(parsed.Transcript, 'hello');
    assert.equal(parsed.AccessToken, REDACTED);
    assert.equal(parsed.IdToken, REDACTED);
    assert.equal(parsed.RefreshToken, REDACTED);
    assertNoTokenRun(rendered, DUMMY_TOKEN, 'KDS record');
    assertNoTokenRun(rendered, DUMMY_REFRESH, 'KDS record');
});

test('redactCallMetaData leaves the original object untouched', () => {
    // The caller still needs the real tokens to start Transcribe; redaction is for
    // the log line only.
    const callMetaData = { callId: 'call-1', accessToken: DUMMY_TOKEN };
    redactCallMetaData(callMetaData);
    assert.equal(callMetaData.accessToken, DUMMY_TOKEN);
});

test('a field that was absent stays absent rather than becoming a placeholder', () => {
    const rendered = redactCallMetaData({ callId: 'call-1' });
    assert.ok(!('accessToken' in rendered));
});

test('the log line jwtVerifier emits for a real request contains no token substring', async () => {
    // End-to-end through the actual verifier rather than the helpers alone, so the
    // test fails if a call site is ever rewritten to interpolate request.url or
    // request.headers again. The logger here is a plain capture stub: what is under
    // test is the message string the verifier composes, which is the same string
    // whatever logger receives it. The real pino path is covered separately below.
    //
    // CognitoJwtVerifier.create() validates the user pool id at module load, so a
    // syntactically valid placeholder has to be in place before the import. The
    // request below carries a malformed authorization value, which takes the
    // "no Bearer token" branch: that branch logs the same request description as
    // the success path and, unlike the success path, needs no network access to
    // reach.
    process.env['USERPOOL_ID'] = process.env['USERPOOL_ID'] || 'us-east-1_aBcDeFgHi';
    const { jwtVerifier } = await import('./jwt-verifier');

    const lines: string[] = [];
    const record = (message: string) => {
        lines.push(message);
    };
    const request = {
        // The token appears in every carrier the API accepts: the query string, the
        // authorization header, and the id_token / refresh_token headers.
        url: `/api/v1/ws?authorization=Bearer%20${DUMMY_TOKEN}&id_token=${DUMMY_TOKEN}&refresh_token=${DUMMY_REFRESH}`,
        query: { authorization: DUMMY_TOKEN },
        headers: {
            host: 'lma.example.com',
            'x-forwarded-for': '203.0.113.7',
            authorization: DUMMY_TOKEN,
            id_token: DUMMY_TOKEN,
            refresh_token: DUMMY_REFRESH,
        },
        log: { error: record, warn: record, info: record, debug: record },
    };
    const reply = { status: () => ({ send: () => undefined }) };

    await jwtVerifier(
        request as unknown as Parameters<typeof jwtVerifier>[0],
        reply as unknown as Parameters<typeof jwtVerifier>[1],
    );

    const emitted = lines.join('\n');
    assert.ok(emitted.length > 0, 'expected the verifier to log something');
    assert.ok(emitted.includes('/api/v1/ws'), 'the request path should still be identifiable');
    assert.ok(emitted.includes('203.0.113.7'), 'the client IP should still be identifiable');
    // Whole value and every partial run of it: a truncated token in a log line is
    // still a copy of the token.
    assertNoTokenRun(emitted, DUMMY_TOKEN, 'jwtVerifier log output');
    assertNoTokenRun(emitted, DUMMY_REFRESH, 'jwtVerifier log output');
});

test('the pino redact configuration censors token fields on object-style records', async () => {
    // Covers index.ts's `redact: PINO_REDACT_OPTIONS` through real pino, using the
    // same exported object the server installs. A Fastify logger is built rather
    // than importing pino directly, because that is how the option reaches pino in
    // production and fastify is a declared dependency here.
    const lines: string[] = [];
    const app = fastify({
        logger: {
            level: 'info',
            redact: PINO_REDACT_OPTIONS,
            stream: {
                write: (chunk: string) => {
                    lines.push(chunk);
                },
            },
        },
    });

    app.log.info(
        {
            callMetaData: {
                callId: 'call-1',
                accessToken: DUMMY_TOKEN,
                idToken: DUMMY_TOKEN,
                refreshToken: DUMMY_REFRESH,
            },
            callEvent: {
                CallId: 'call-1',
                AccessToken: DUMMY_TOKEN,
                IdToken: DUMMY_TOKEN,
                RefreshToken: DUMMY_REFRESH,
            },
            headers: { authorization: `Bearer ${DUMMY_TOKEN}`, cookie: 'session=abc' },
        },
        'object-style record',
    );
    await app.close();

    const emitted = lines.join('\n');
    assert.ok(emitted.includes('call-1'), 'the call id should survive redaction');
    assert.ok(emitted.includes(REDACTED), 'expected the censor placeholder in the record');
    assertNoTokenRun(emitted, DUMMY_TOKEN, 'pino redact');
    assertNoTokenRun(emitted, DUMMY_REFRESH, 'pino redact');
});

test('the pino redact configuration cannot reach an interpolated message string', async () => {
    // Pins the reason the call-site helpers exist: pino rewrites properties of the
    // logged object and never inspects the rendered message, so a token that has
    // already been interpolated into a string passes straight through. If a future
    // pino ever did censor message text, this test fails and the redundant
    // call-site redaction can be reconsidered — it must not be removed on the
    // assumption that the config covers it.
    const lines: string[] = [];
    const app = fastify({
        logger: {
            level: 'info',
            redact: PINO_REDACT_OPTIONS,
            stream: {
                write: (chunk: string) => {
                    lines.push(chunk);
                },
            },
        },
    });

    app.log.info(`accessToken=${DUMMY_TOKEN}`);
    await app.close();

    assert.ok(lines.join('\n').includes(DUMMY_TOKEN));
});
