/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * One meeting's consent session against a fake consent API.
 *
 * - It waits for the meeting's row, then records the step: start, the notice,
 *   and in opt-in the owner's attestation.
 * - An opt-in meeting opens its gate only on the record's CaptureAllowed, and
 *   stops when the step cannot be recorded; a notify meeting stays open.
 * - An objection closes the gate before it is written, and is written.
 * - Evidence is stored under the meeting's folder, added, then checked; a
 *   check that fails leaves it stored.
 * - The step is recorded only on a meeting the verified caller owns.
 * - The signed mutations select the whole record, and only the check selects
 *   its result.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { ADD_EVIDENCE, ConsentApi, GraphqlCaller, START, VERIFY_EVIDENCE } from './appsync';
import { parseClientConsent } from './rules';
import { ConsentSession } from './session';

type Call = { field: string; input: Record<string, unknown> };

const last = <T>(items: readonly T[]): T | undefined => items[items.length - 1];

const FIELD = /(?:mutation|query) \(\$\w+: \w+!\) \{\s*(\w+)\(/;

/** A fake API answering like the resolver, over one in-memory record. */
const fakeApi = (
    options: { missingFor?: number; failing?: string[]; objected?: boolean; owner?: string } = {}
) => {
    const calls: Call[] = [];
    let missing = options.missingFor ?? 0;
    const record: Record<string, unknown> = { Events: [], Evidence: [], StatusHistory: [] };
    const caller: GraphqlCaller = async (query, variables) => {
        const field = FIELD.exec(query)?.[1] ?? '';
        const input = (variables['input'] ?? variables) as Record<string, unknown>;
        calls.push({ field, input });
        if (options.failing?.includes(field)) {
            return { errors: [{ message: `${field} refused` }] };
        }
        if (field === 'getCall') {
            // null until the call event processor has created the meeting's row.
            if (missing > 0) {
                missing -= 1;
                return { data: { getCall: null } };
            }
            return { data: { getCall: { CallId: input['CallId'], Owner: options.owner ?? 'ana' } } };
        }
        if (field === 'startMeetingConsent') {
            record['Mode'] = input['Mode'];
            if (options.objected) {
                record['Status'] = 'objected';
            }
        } else if (field === 'recordMeetingConsentNotice') {
            record['NoticePostedAt'] = '2026-11-02T10:00:00.000Z';
        } else if (field === 'attestMeetingConsent') {
            record['Attestation'] = { Method: input['Method'] };
        } else if (field === 'recordMeetingConsentEvent' && input['Decision'] === 'objection') {
            record['Status'] = 'objected';
        } else if (field === 'addMeetingConsentEvidence') {
            (record['Evidence'] as unknown[]).push({ EvidenceId: 'ev1', S3Key: input['S3Key'] });
        } else if (field === 'verifyMeetingConsentEvidence') {
            record['Evidence'] = [{ EvidenceId: 'ev1', S3Key: 'k', Verification: { Verdict: 'supports_consent' } }];
        }
        const mode = record['Mode'];
        const status =
            record['Status'] === 'objected'
                ? 'objected'
                : mode === 'notify'
                    ? record['NoticePostedAt'] ? 'granted' : 'pending'
                    : record['Attestation'] ? 'granted' : 'pending';
        record['Status'] = status;
        record['CaptureAllowed'] = status === 'granted' || (mode === 'notify' && status !== 'objected');
        record['StatusHistory'] = [{ Reason: `status ${status}` }];
        return { data: { [field]: { CallId: input['CallId'], Owner: options.owner ?? 'ana', Consent: { ...record } } } };
    };
    return { api: new ConsentApi(caller), calls };
};

const harness = (api: ConsentApi | undefined) => {
    const frames: Array<Record<string, unknown>> = [];
    const gates: Array<{ open: boolean; reason: string; discard?: boolean }> = [];
    const stored: Array<{ key: string; contentType: string }> = [];
    const logs: string[] = [];
    const deps = {
        api,
        callerUsername: 'ana' as string | undefined,
        putEvidence: async (key: string, _bytes: Buffer, contentType: string) => {
            stored.push({ key, contentType });
        },
        evidencePrefix: 'lma-consent-evidence/',
        send: (frame: Record<string, unknown>) => frames.push(frame),
        onGate: (open: boolean, reason: string, discard?: boolean) =>
            gates.push({ open, reason, ...(discard ? { discard } : {}) }),
        log: { info: (m: string) => logs.push(m), warn: (m: string) => logs.push(m), error: (m: string) => logs.push(m) },
        sleep: async () => undefined,
        meetingWaitMs: 60_000,
    };
    return { deps, frames, gates, stored, logs };
};

const OPT_IN = { mode: 'opt_in' as const, rule: 'owner_attests' as const, gateOpen: false };
const NOTIFY = { mode: 'notify' as const, rule: null, gateOpen: true };
const confirmed = (mode: string, method = 'verbal') =>
    parseClientConsent({ mode, confirmed: true, method, noticeText: 'Ana is recording', lmaUser: 'Ana', meetingName: 'Sync', client: 'Stream Audio' })!;

test('an opt-in meeting opens only once its attestation is recorded, after waiting for the meeting', async () => {
    const { api, calls } = fakeApi({ missingFor: 2 });
    const h = harness(api);
    const session = new ConsentSession('Sync - 1', OPT_IN, confirmed('opt_in'), h.deps);
    assert.equal(session.gateOpen, false);
    await session.begin();
    assert.deepEqual(
        calls.map((c) => c.field),
        ['getCall', 'getCall', 'getCall', 'startMeetingConsent', 'recordMeetingConsentNotice', 'attestMeetingConsent']
    );
    assert.deepEqual(calls[0]?.input, { CallId: 'Sync - 1' });
    assert.deepEqual(calls[3]?.input, { CallId: 'Sync - 1', Mode: 'opt_in', Rule: 'owner_attests', LmaUser: 'Ana', MeetingName: 'Sync' });
    assert.deepEqual(calls[4]?.input, { CallId: 'Sync - 1', Channel: 'voice', Text: 'Ana is recording' });
    assert.equal(calls[5]?.input['Method'], 'verbal');
    assert.match(String(calls[5]?.input['Statement']), /in Stream Audio/);
    assert.deepEqual(h.gates, [{ open: true, reason: 'status granted' }]);
    assert.equal(session.gateOpen, true);
    assert.equal(last(h.frames)?.['captureAllowed'], true);
});

test('an opt-in meeting whose attestation is refused is never captured', async () => {
    const { api } = fakeApi({ failing: ['attestMeetingConsent'] });
    const h = harness(api);
    const session = new ConsentSession('m', OPT_IN, confirmed('opt_in'), h.deps);
    await session.begin();
    assert.equal(session.gateOpen, false);
    assert.equal(session.isStopped, true);
    assert.deepEqual(h.gates.map((g) => g.open), [false]);
    assert.equal(last(h.frames)?.['code'], 'CONSENT_NOT_RECORDED');
});

test('an opt-in meeting is not captured when the meeting never appears, or there is no API', async () => {
    const { api } = fakeApi({ missingFor: 1000 });
    const h = harness(api);
    const session = new ConsentSession('m', OPT_IN, confirmed('opt_in'), { ...h.deps, meetingWaitMs: 0 });
    await session.begin();
    assert.equal(session.isStopped, true);
    assert.match(String(last(h.frames)?.['message']), /not created in time/);

    const none = harness(undefined);
    const unconfigured = new ConsentSession('m', OPT_IN, confirmed('opt_in'), none.deps);
    await unconfigured.begin();
    assert.equal(unconfigured.isStopped, true);
});

test('an opt-in meeting is not captured when its owner cannot be read', async () => {
    const { api, calls } = fakeApi({ failing: ['getCall'] });
    const h = harness(api);
    const session = new ConsentSession('m', OPT_IN, confirmed('opt_in'), h.deps);
    await session.begin();
    assert.deepEqual(calls.map((c) => c.field), ['getCall']);
    assert.equal(session.isStopped, true);
    assert.match(String(last(h.frames)?.['message']), /owner could not be read/);
});

test('a step decided without the settings is not captured when its record is refused', async () => {
    const { api } = fakeApi({ failing: ['startMeetingConsent'] });
    const h = harness(api);
    const decision = { mode: 'notify' as const, rule: null, gateOpen: false, requireRecord: true };
    const session = new ConsentSession('m', decision, confirmed('notify', 'chat'), h.deps);
    assert.equal(session.gateOpen, false);
    await session.begin();
    assert.equal(session.isStopped, true);
    assert.equal(last(h.frames)?.['captureAllowed'], false);
});

test('a step decided without the settings opens when its record allows capture', async () => {
    const { api } = fakeApi();
    const h = harness(api);
    const decision = { mode: 'notify' as const, rule: null, gateOpen: false, requireRecord: true };
    const session = new ConsentSession('m', decision, confirmed('notify', 'chat'), h.deps);
    await session.begin();
    assert.deepEqual(h.gates.map((g) => g.open), [true]);
    assert.equal(session.gateOpen, true);
});

test('a notify meeting is open from the start, and stays open when its record cannot be written', async () => {
    const { api } = fakeApi({ failing: ['startMeetingConsent'] });
    const h = harness(api);
    const session = new ConsentSession('m', NOTIFY, confirmed('notify', 'chat'), h.deps);
    assert.equal(session.gateOpen, true);
    await session.begin();
    assert.equal(session.gateOpen, true);
    assert.deepEqual(h.gates, []);
    assert.equal(last(h.frames)?.['code'], 'CONSENT_NOT_RECORDED');
});

test('a notice that cannot be recorded is reported, and a notify meeting goes on', async () => {
    const { api } = fakeApi({ failing: ['recordMeetingConsentNotice'] });
    const h = harness(api);
    const session = new ConsentSession('m', NOTIFY, confirmed('notify', 'chat'), h.deps);
    await session.begin();
    assert.equal(last(h.frames)?.['code'], 'NOTICE_NOT_RECORDED');
    assert.equal(last(h.frames)?.['captureAllowed'], true);
    assert.equal(session.gateOpen, true);
});

test('an unconfirmed notify step starts the record without recording a notice', async () => {
    const { api, calls } = fakeApi();
    const h = harness(api);
    const step = parseClientConsent({ mode: 'notify', confirmed: false })!;
    await new ConsentSession('m', NOTIFY, step, h.deps).begin();
    assert.deepEqual(calls.map((c) => c.field), ['getCall', 'startMeetingConsent']);
    assert.equal(last(h.frames)?.['status'], 'pending');
    assert.equal(last(h.frames)?.['captureAllowed'], true);
});

test('a reconnect to a meeting somebody objected to stays stopped and attests nothing', async () => {
    const { api, calls } = fakeApi({ objected: true });
    const h = harness(api);
    const session = new ConsentSession('m', OPT_IN, confirmed('opt_in'), h.deps);
    await session.begin();
    assert.deepEqual(calls.map((c) => c.field), ['getCall', 'startMeetingConsent']);
    assert.equal(session.isStopped, true);
});

test('an objection stops capture before it is written, and is written', async () => {
    const { api, calls } = fakeApi();
    const h = harness(api);
    const session = new ConsentSession('m', NOTIFY, confirmed('notify'), h.deps);
    await session.begin();
    const pending = session.objection({ participantName: 'Bo Chen', source: 'chat', text: 'I object' });
    assert.equal(session.gateOpen, false);
    assert.deepEqual(h.gates.map((g) => g.open), [false]);
    assert.equal(last(h.frames)?.['code'], 'OBJECTION_RECEIVED');
    await pending;
    const event = last(calls);
    assert.equal(event?.field, 'recordMeetingConsentEvent');
    assert.equal(event?.input['ParticipantName'], 'Bo Chen');
    assert.equal(event?.input['Decision'], 'objection');
    assert.equal(event?.input['Source'], 'chat');
    // Only one close, however many writes answer "not allowed".
    assert.deepEqual(h.gates.map((g) => g.open), [false]);
});

test('an objection while the meeting is being created is recorded, and nothing is confirmed after it', async () => {
    const { api, calls } = fakeApi({ missingFor: 2 });
    const h = harness(api);
    const session = new ConsentSession('m', OPT_IN, confirmed('opt_in'), h.deps);
    void session.begin();
    void session.objection({ participantName: 'Bo Chen' });
    await session.settled();
    assert.deepEqual(
        calls.map((c) => c.field),
        ['getCall', 'getCall', 'getCall', 'startMeetingConsent', 'recordMeetingConsentEvent']
    );
    assert.deepEqual(h.gates.map((g) => g.open), [false]);
});

test('an objection names somebody and a source even when the client did not', async () => {
    const { api, calls } = fakeApi();
    const h = harness(api);
    const session = new ConsentSession('m', NOTIFY, confirmed('notify'), h.deps);
    await session.begin();
    await session.objection({ source: 'telepathy' });
    assert.equal(last(calls)?.input['ParticipantName'], 'A participant');
    assert.equal(last(calls)?.input['Source'], 'voice');
});

const JPEG_B64 = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]).toString('base64');

test('evidence is stored under the meeting folder, added to the record, then checked', async () => {
    const { api, calls } = fakeApi();
    const h = harness(api);
    const session = new ConsentSession('Sync - 1', OPT_IN, confirmed('opt_in'), h.deps);
    void session.begin();
    await session.evidence({ image: JPEG_B64, capturedAt: '2026-11-02T10:01:30Z', note: 'meeting window' });
    assert.equal(h.stored.length, 1);
    assert.match(h.stored[0]?.key ?? '', /^lma-consent-evidence\/Sync___1\/screenshot-\d{8}T\d{6}Z-[0-9a-f]{8}\.jpg$/);
    assert.equal(h.stored[0]?.contentType, 'image/jpeg');
    const add = calls.find((c) => c.field === 'addMeetingConsentEvidence');
    assert.equal(add?.input['S3Key'], h.stored[0]?.key);
    assert.equal(add?.input['CapturedAt'], '2026-11-02T10:01:30.000Z');
    assert.deepEqual(last(calls), { field: 'verifyMeetingConsentEvidence', input: { CallId: 'Sync - 1', EvidenceId: 'ev1' } });
    assert.deepEqual(last(h.frames)?.['evidence'], { evidenceId: 'ev1', stored: true, verdict: 'supports_consent' });
});

test('evidence stays stored and unchecked when its check fails', async () => {
    const { api } = fakeApi({ failing: ['verifyMeetingConsentEvidence'] });
    const h = harness(api);
    const session = new ConsentSession('m', NOTIFY, confirmed('notify'), h.deps);
    void session.begin();
    await session.evidence({ image: JPEG_B64 });
    assert.deepEqual(last(h.frames)?.['evidence'], { evidenceId: 'ev1', stored: true });
});

test('evidence that is not an image, or beyond the per-meeting limit, is refused without a write', async () => {
    const { api, calls } = fakeApi();
    const h = harness(api);
    const session = new ConsentSession('m', NOTIFY, confirmed('notify'), h.deps);
    await session.begin();
    const before = calls.length;
    await session.evidence({ image: Buffer.from('<svg/>').toString('base64') });
    assert.equal(last(h.frames)?.['code'], 'EVIDENCE_REFUSED');
    assert.equal(calls.length, before);
    // A refused image does not count: five images are still accepted.
    for (let i = 0; i < 6; i += 1) {
        await session.evidence({ image: JPEG_B64 });
    }
    assert.equal(last(h.frames)?.['code'], 'EVIDENCE_REFUSED');
    assert.equal(h.stored.length, 5);
});

test('the step is not recorded on another user\'s meeting, and capture stops', async () => {
    const { api, calls } = fakeApi({ owner: 'bo' });
    const h = harness(api);
    const session = new ConsentSession('m', NOTIFY, confirmed('notify'), h.deps);
    await session.begin();
    await session.evidence({ image: JPEG_B64 });
    await session.objection({ participantName: 'X' });
    // Only the owner was read: nothing was written to the record.
    assert.deepEqual(calls.map((c) => c.field), ['getCall']);
    assert.equal(h.stored.length, 0);
    assert.equal(session.isStopped, true);
    // Capture of another user's meeting is discarded, not ended.
    assert.deepEqual(h.gates, [{ open: false, reason: 'the meeting belongs to another user', discard: true }]);
    assert.match(String(h.frames.find((f) => f['code'] === 'MEETING_NOT_OWNED')?.['message']), /another user/);
});

test('a caller without a username records nothing', async () => {
    const { api, calls } = fakeApi();
    const h = harness(api);
    const session = new ConsentSession('m', OPT_IN, confirmed('opt_in'), { ...h.deps, callerUsername: undefined });
    await session.begin();
    assert.deepEqual(calls.map((c) => c.field), ['getCall']);
    assert.equal(session.gateOpen, false);
    assert.equal(session.isStopped, true);
});

test('every mutation selects the whole record, and only the check selects its result', () => {
    for (const document of [START, ADD_EVIDENCE]) {
        assert.match(document, /StatusHistory \{ Status CaptureAllowed At Reason \}/);
        assert.match(document, /Evidence \{ EvidenceId Kind S3Key CapturedAt AddedAt Note SignedUrl \}/);
        assert.doesNotMatch(document, /Verification/);
    }
    assert.match(VERIFY_EVIDENCE, /Verification \{ Verdict /);
    assert.match(VERIFY_EVIDENCE, /verifyMeetingConsentEvidence\(input: \$input\)/);
});
