/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    resolveLaunchType,
    isMicrovmLaunch,
    requiresAlbSelfRegistration,
    buildRunHookPayload,
    parseRunHookPayload,
    applyPerMeetingConfig,
    redactPerMeetingConfig,
    microvmVncEndpoint,
    RUN_HOOK_PAYLOAD_MAX_BYTES,
    PER_MEETING_KEYS,
} from './launch-mode.js';

test('resolveLaunchType recognises all three launch types, case/space insensitively', () => {
    assert.equal(resolveLaunchType('MICROVM'), 'MICROVM');
    assert.equal(resolveLaunchType('microvm'), 'MICROVM');
    assert.equal(resolveLaunchType('  MicroVM  '), 'MICROVM');
    assert.equal(resolveLaunchType('EC2'), 'EC2');
    assert.equal(resolveLaunchType('ec2'), 'EC2');
    assert.equal(resolveLaunchType('FARGATE'), 'FARGATE');
});

test('resolveLaunchType defaults to FARGATE for absent/unknown values', () => {
    // Matches the pre-existing scheduler-Lambda default so adding this module
    // cannot change behaviour for stacks that do not set VP_LAUNCH_TYPE.
    assert.equal(resolveLaunchType(undefined), 'FARGATE');
    assert.equal(resolveLaunchType(''), 'FARGATE');
    assert.equal(resolveLaunchType('nonsense'), 'FARGATE');
});

test('ALB self-registration is required for ECS launch types but not MICROVM', () => {
    assert.equal(requiresAlbSelfRegistration('EC2'), true);
    assert.equal(requiresAlbSelfRegistration('FARGATE'), true);
    assert.equal(requiresAlbSelfRegistration(undefined), true);
    assert.equal(requiresAlbSelfRegistration('MICROVM'), false);
    assert.equal(isMicrovmLaunch('MICROVM'), true);
    assert.equal(isMicrovmLaunch('EC2'), false);
});

test('buildRunHookPayload round-trips through parseRunHookPayload', () => {
    const config = {
        VIRTUAL_PARTICIPANT_ID: 'vp-123',
        MEETING_PLATFORM: 'Teams',
        MEETING_ID: '243574196567966',
        MEETING_NAME: 'Weekly sync',
        MEETING_TIME: '1786129399',
        LMA_USER: 'bob@example.com',
        USER_ACCESS_TOKEN: 'aaa.bbb.ccc',
        ENABLE_VIDEO_RECORDING: 'false',
    };
    const payload = buildRunHookPayload(config);
    const parsed = parseRunHookPayload({ microvmId: 'mvm-1', runHookPayload: payload });
    assert.deepEqual(parsed, config);
});

test('buildRunHookPayload omits empty values so app-side defaults still apply', () => {
    const payload = buildRunHookPayload({
        VIRTUAL_PARTICIPANT_ID: 'vp-1',
        MEETING_PASSWORD: '',
        MEETING_NAME: undefined,
    });
    const parsed = JSON.parse(payload) as Record<string, string>;
    assert.deepEqual(Object.keys(parsed), ['VIRTUAL_PARTICIPANT_ID']);
});

test('buildRunHookPayload throws rather than truncating an oversized payload', () => {
    // Three real Cognito JWTs are the realistic worst case; simulate with
    // values that together exceed the 16 KB service limit.
    const big = 'x'.repeat(7000);
    assert.throws(
        () =>
            buildRunHookPayload({
                USER_ACCESS_TOKEN: big,
                USER_ID_TOKEN: big,
                USER_REFRESH_TOKEN: big,
            }),
        /exceeding the 16384-byte limit/,
    );
});

test('buildRunHookPayload accepts a realistic three-JWT payload', () => {
    // Guards the "will 3 JWTs fit?" question with a concrete number: Cognito
    // access/id tokens run ~1-1.5 KB, refresh tokens ~1-2 KB.
    const jwt = 'h.'.padEnd(1500, 'p') + '.s';
    const payload = buildRunHookPayload({
        VIRTUAL_PARTICIPANT_ID: 'vp-abc',
        MEETING_PLATFORM: 'Zoom',
        MEETING_ID: '999 888 7777',
        MEETING_PASSWORD: 'secret',
        MEETING_NAME: 'Quarterly review with a fairly long meeting name',
        MEETING_TIME: '1786129399',
        LMA_USER: 'someone@example.com',
        LMA_USER_SUB: '11111111-2222-3333-4444-555555555555',
        USER_ACCESS_TOKEN: jwt,
        USER_ID_TOKEN: jwt,
        USER_REFRESH_TOKEN: jwt,
        ZOOM_CREDENTIALS_SECRET_NAME: 'LMA-Stack/zoom-credentials/abc',
        ENABLE_VIDEO_RECORDING: 'true',
    });
    assert.ok(Buffer.byteLength(payload, 'utf8') < RUN_HOOK_PAYLOAD_MAX_BYTES);
});

test('parseRunHookPayload is tolerant of malformed input', () => {
    // The container must still boot and publish a legible failure rather than
    // crash-looping before any status reaches the UI.
    assert.deepEqual(parseRunHookPayload(undefined), {});
    assert.deepEqual(parseRunHookPayload(''), {});
    assert.deepEqual(parseRunHookPayload('not json'), {});
    assert.deepEqual(parseRunHookPayload({}), {});
    assert.deepEqual(parseRunHookPayload({ microvmId: 'm' }), {});
    assert.deepEqual(parseRunHookPayload({ runHookPayload: 'not json' }), {});
    assert.deepEqual(parseRunHookPayload({ runHookPayload: '[]' }), {});
    assert.deepEqual(parseRunHookPayload({ runHookPayload: 'null' }), {});
    assert.deepEqual(parseRunHookPayload({ runHookPayload: '"str"' }), {});
});

test('parseRunHookPayload accepts a JSON string body (raw POST body)', () => {
    const body = JSON.stringify({ microvmId: 'm', runHookPayload: '{"MEETING_ID":"42"}' });
    assert.deepEqual(parseRunHookPayload(body), { MEETING_ID: '42' });
});

test('parseRunHookPayload coerces numbers and booleans to strings', () => {
    // Env vars are always strings; a launcher may send MEETING_TIME as a number
    // or ENABLE_VIDEO_RECORDING as a bool.
    const parsed = parseRunHookPayload({
        runHookPayload: '{"MEETING_TIME":1786129399,"ENABLE_VIDEO_RECORDING":false}',
    });
    assert.equal(parsed.MEETING_TIME, '1786129399');
    assert.equal(parsed.ENABLE_VIDEO_RECORDING, 'false');
});

test('parseRunHookPayload ignores keys outside the known per-meeting set', () => {
    const parsed = parseRunHookPayload({
        runHookPayload: '{"MEETING_ID":"1","AWS_SECRET_ACCESS_KEY":"nope","PATH":"/evil"}',
    });
    // Prevents a launcher bug (or a tampered payload) from injecting arbitrary
    // environment variables into the container.
    assert.deepEqual(parsed, { MEETING_ID: '1' });
});

test('applyPerMeetingConfig sets only missing keys by default', () => {
    const env: Record<string, string | undefined> = { MEETING_ID: 'preset' };
    const applied = applyPerMeetingConfig(env, { MEETING_ID: 'from-hook', MEETING_NAME: 'Standup' });
    assert.equal(env.MEETING_ID, 'preset', 'existing values must win by default');
    assert.equal(env.MEETING_NAME, 'Standup');
    assert.deepEqual(applied, ['MEETING_NAME']);
});

test('applyPerMeetingConfig overwrites when asked', () => {
    const env: Record<string, string | undefined> = { MEETING_ID: 'preset' };
    applyPerMeetingConfig(env, { MEETING_ID: 'from-hook' }, { overwrite: true });
    assert.equal(env.MEETING_ID, 'from-hook');
});

test('applyPerMeetingConfig treats empty existing values as unset', () => {
    const env: Record<string, string | undefined> = { MEETING_NAME: '' };
    applyPerMeetingConfig(env, { MEETING_NAME: 'Real name' });
    assert.equal(env.MEETING_NAME, 'Real name');
});

test('microvmVncEndpoint builds a wss URL from the RunMicrovm endpoint', () => {
    assert.equal(
        microvmVncEndpoint('abc123.lambda-microvm.us-west-2.on.aws'),
        'wss://abc123.lambda-microvm.us-west-2.on.aws',
    );
});

test('microvmVncEndpoint normalises schemes, trailing slashes and whitespace', () => {
    const want = 'wss://abc.lambda-microvm.us-west-2.on.aws';
    assert.equal(microvmVncEndpoint('  abc.lambda-microvm.us-west-2.on.aws  '), want);
    assert.equal(microvmVncEndpoint('wss://abc.lambda-microvm.us-west-2.on.aws'), want);
    assert.equal(microvmVncEndpoint('ws://abc.lambda-microvm.us-west-2.on.aws'), want);
    assert.equal(microvmVncEndpoint('abc.lambda-microvm.us-west-2.on.aws/'), want);
});

test('microvmVncEndpoint returns empty string when the endpoint is missing', () => {
    // Callers treat '' like the existing missing-CLOUDFRONT_DOMAIN case rather
    // than publishing a malformed URL the viewer would fail on.
    assert.equal(microvmVncEndpoint(undefined), '');
    assert.equal(microvmVncEndpoint(''), '');
    assert.equal(microvmVncEndpoint('   '), '');
});

test('redactPerMeetingConfig hides tokens and passwords but keeps shape', () => {
    const red = redactPerMeetingConfig({
        MEETING_ID: '42',
        MEETING_PASSWORD: 'hunter2',
        USER_ACCESS_TOKEN: 'aaa.bbb.ccc',
        USER_ID_TOKEN: 'ddd',
        USER_REFRESH_TOKEN: 'eee',
    });
    assert.equal(red.MEETING_ID, '42');
    assert.equal(red.MEETING_PASSWORD, '<redacted:7>');
    assert.equal(red.USER_ACCESS_TOKEN, '<redacted:11>');
    assert.ok(!JSON.stringify(red).includes('hunter2'));
    assert.ok(!JSON.stringify(red).includes('aaa.bbb.ccc'));
});

test('PER_MEETING_KEYS covers every value the ECS launchers override', () => {
    // Keeps this module in sync with the three existing dispatch paths
    // (Step Functions RunTask, EventBridge Scheduler, scheduler Lambda).
    // If someone adds an override there, this list must grow too.
    for (const k of [
        'VIRTUAL_PARTICIPANT_ID',
        'MEETING_PLATFORM',
        'MEETING_ID',
        'MEETING_PASSWORD',
        'MEETING_NAME',
        'MEETING_TIME',
        'LMA_USER',
        'LMA_USER_SUB',
        'USER_ACCESS_TOKEN',
        'USER_ID_TOKEN',
        'USER_REFRESH_TOKEN',
        'ZOOM_CREDENTIALS_SECRET_NAME',
        'ENABLE_VIDEO_RECORDING',
    ]) {
        assert.ok(
            (PER_MEETING_KEYS as readonly string[]).includes(k),
            `${k} missing from PER_MEETING_KEYS`,
        );
    }
});

test('microvmVncEndpoint accepts an already-wss endpoint from the registry', () => {
    // The launcher writes `wss://<host>` into the VP task registry, and the
    // container reads it back from there — the endpoint only exists AFTER
    // RunMicrovm returns, so it cannot travel in the /run payload (which is
    // sent as part of that same call).
    assert.equal(
        microvmVncEndpoint('wss://abc.lambda-microvm.us-west-2.on.aws'),
        'wss://abc.lambda-microvm.us-west-2.on.aws',
    );
});
