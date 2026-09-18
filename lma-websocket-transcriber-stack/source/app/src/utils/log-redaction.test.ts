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
 * The end-to-end assertion is `the emitted log line contains no token
 * substring`, which drives a real pino logger through jwtVerifier and searches
 * the captured output.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    LOGGABLE_HEADER_NAMES,
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

    assert.ok(summary.includes('lma.example.com'));
    assert.ok(summary.includes('test-agent'));
    // Four non-allowlisted headers were present and are reported as a count only.
    assert.ok(summary.includes('(+4 more)'), `unexpected summary: ${summary}`);
    assert.ok(!summary.includes(DUMMY_TOKEN));
    assert.ok(!summary.includes(DUMMY_REFRESH));
    assert.ok(!summary.includes('session=abc'));
});

test('a header name is matched case-insensitively against the allowlist', () => {
    // Node lowercases incoming header names, but the helper is also used on plain
    // objects in tests and smoke scripts.
    assert.ok(summarizeHeaders({ Host: 'lma.example.com' }).includes('lma.example.com'));
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
    assert.ok(!description.includes(DUMMY_TOKEN));
    // A partial match would also be a leak, so check a distinctive fragment too.
    assert.ok(!description.includes('dW1teS1jYW5hcnk'));
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
    assert.ok(!rendered.includes(DUMMY_TOKEN));
    assert.ok(!rendered.includes(DUMMY_REFRESH));
    assert.equal(JSON.parse(rendered).accessToken, REDACTED);
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
    // request.headers again.
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
    assert.ok(!emitted.includes(DUMMY_TOKEN), `token present in log output: ${emitted}`);
    assert.ok(!emitted.includes(DUMMY_REFRESH), `refresh token present in log output: ${emitted}`);
    // A partial match would be just as bad, so check a distinctive fragment too.
    assert.ok(!emitted.includes('dW1teS1jYW5hcnk'), `token fragment present: ${emitted}`);
});
