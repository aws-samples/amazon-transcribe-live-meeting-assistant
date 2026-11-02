/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Virtual Participant's consent flow (meeting-consent.ts), per mode and
 * rule, against a fake consent API that decides the record's status the way
 * the meeting consent resolver does (lma_consent.evaluate), with the capture
 * switch, the chat, the screenshots and the uploads recorded.
 *
 * What is not covered here: each platform's chat and attendee scraping in a
 * live meeting, and the AI stack's own resolver, which its Python suites test.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import type { ConsentApi, ConsentRecord, RecordEventInput, StartConsentInput } from './consent-api.js';
import { ConsentApiError } from './consent-api.js';
import {
    ConsentFlowDeps,
    MAX_EVIDENCE_IMAGES,
    MeetingConsentFlow,
    describeWaiting,
    effectiveConsentSettings,
    planConsent,
} from './meeting-consent.js';
import { normalizeParticipantName } from './consent-matcher.js';
import { noteParticipantSeen, UNIDENTIFIED_PARTICIPANT } from './consent-roster.js';

// ─── A fake API with the resolver's rules ───────────────────────────────────

function evaluate(record: ConsentRecord): { status: string; allowed: boolean } {
    if (record.Mode === 'off') return { status: 'not_required', allowed: true };
    const latest = new Map<string, string>();
    for (const event of record.Events ?? []) {
        if (event.Source === 'attestation') continue;
        latest.set(normalizeParticipantName(event.ParticipantName), event.Decision as string);
    }
    if ([...latest.values()].includes('objection')) return { status: 'objected', allowed: false };
    if (record.Mode === 'notify') return { status: record.NoticePostedAt ? 'granted' : 'pending', allowed: true };
    if (record.Rule === 'owner_attests') return record.Attestation ? { status: 'granted', allowed: true } : { status: 'pending', allowed: false };
    const present = (record.Participants ?? []).map(normalizeParticipantName);
    if (present.length === 0) return { status: 'pending', allowed: false };
    return present.every((name) => latest.get(name) === 'consent') ? { status: 'granted', allowed: true } : { status: 'pending', allowed: false };
}

class FakeApi implements ConsentApi {
    record: ConsentRecord | null = null;
    calls: string[] = [];
    starts: StartConsentInput[] = [];
    verified: string[] = [];
    notCreatedFor = 0;
    refuseChoice = false;
    private id = 0;

    constructor(
        private readonly deployment: { mode: string; rule: string },
        private readonly owner = 'ana',
    ) {}

    private write(change: (record: ConsentRecord) => void): ConsentRecord {
        const record = this.record as ConsentRecord;
        change(record);
        const { status, allowed } = evaluate(record);
        record.Status = status;
        record.CaptureAllowed = allowed;
        record.Version = (record.Version ?? 0) + 1;
        return JSON.parse(JSON.stringify(record));
    }

    async start(input: StartConsentInput): Promise<ConsentRecord | null> {
        this.calls.push('start');
        this.starts.push(input);
        if (this.notCreatedFor > 0) {
            this.notCreatedFor -= 1;
            throw new ConsentApiError('no meeting m-1', 'BadRequestError');
        }
        if (this.refuseChoice && input.Mode) throw new ConsentApiError('this deployment does not allow a meeting to use less consent than its default (notify)', 'Lambda:Unhandled');
        if (this.record) return JSON.parse(JSON.stringify(this.record));
        const mode = input.Mode ?? this.deployment.mode;
        this.record = {
            Mode: mode,
            Rule: mode === 'opt_in' ? input.Rule ?? this.deployment.rule : null,
            NoticeText: mode === 'opt_in' ? 'Reply "I consent" to agree, or "I object" to decline.' : 'Reply "I object" to stop it.',
            Participants: [],
            Events: [],
            Evidence: [],
            Version: 0,
        };
        return this.write(() => {});
    }

    async get() {
        this.calls.push('get');
        return {
            record: this.record ? JSON.parse(JSON.stringify(this.record)) : null,
            captureAllowed: this.record?.CaptureAllowed === true,
            settings: { ConsentPhrases: ['I consent', 'I agree'], ObjectionPhrases: ['I object', 'I do not consent', 'stop recording'] },
        };
    }

    noticeTexts: string[] = [];

    async recordNotice(text: string): Promise<ConsentRecord | null> {
        this.calls.push('recordNotice');
        this.noticeTexts.push(text);
        return this.write((record) => {
            record.NoticePostedAt = record.NoticePostedAt ?? '2026-11-02T10:00:00.000Z';
        });
    }

    async setParticipants(names: string[]): Promise<ConsentRecord | null> {
        this.calls.push(`setParticipants:${names.join('|')}`);
        return this.write((record) => {
            record.Participants = names;
        });
    }

    async recordEvent(input: RecordEventInput): Promise<ConsentRecord | null> {
        this.calls.push(`recordEvent:${input.ParticipantName}:${input.Decision}`);
        return this.write((record) => {
            record.Events = [...(record.Events ?? []), { EventId: `e${++this.id}`, ParticipantName: input.ParticipantName, Decision: input.Decision, Source: 'chat', Text: input.Text }];
        });
    }

    async addEvidence(input: { S3Key: string }): Promise<ConsentRecord | null> {
        this.calls.push(`addEvidence:${input.S3Key.split('-').slice(-2, -1)[0]}`);
        return this.write((record) => {
            record.Evidence = [...(record.Evidence ?? []), { EvidenceId: `v${++this.id}`, S3Key: input.S3Key }];
        });
    }

    async verifyEvidence(evidenceId: string): Promise<void> {
        this.verified.push(evidenceId);
    }

    /** What the meeting page does: the owner attests, or a signed-in user records an objection. */
    attest(): void {
        this.write((record) => {
            record.Attestation = { AttestedBy: this.owner };
            record.Events = [...(record.Events ?? []), { EventId: `e${++this.id}`, ParticipantName: this.owner, Decision: 'consent', Source: 'attestation' }];
        });
    }

    objectFromPage(name: string): void {
        this.write((record) => {
            record.Events = [...(record.Events ?? []), { EventId: `e${++this.id}`, ParticipantName: name, Decision: 'objection', Source: 'ui' }];
        });
    }
}

// ─── The rest of the VP, recorded ───────────────────────────────────────────

interface Harness {
    flow: MeetingConsentFlow;
    api: FakeApi;
    chat: string[];
    details: string[];
    uploads: string[];
    log: string[];
    capture: { on: boolean; releases: number; holds: number; video: string[] };
    left: string[];
    settle: () => Promise<void>;
    /** Moves the flow's clock on. */
    advance: (ms: number) => void;
}

async function harness(options: {
    deployment?: Record<string, unknown>;
    choice?: { mode?: string; rule?: string } | null;
    api?: FakeApi;
    screenshots?: boolean;
    stopVideo?: () => Promise<void>;
    onSleep?: () => void;
    chat?: string[];
    readRoster?: () => Promise<string[] | null>;
    sendersHidden?: boolean;
} = {}): Promise<Harness> {
    const deployment = effectiveConsentSettings(options.deployment ?? { mode: 'notify' });
    const api = options.api ?? new FakeApi({ mode: deployment.mode, rule: deployment.rule });
    const chat: string[] = options.chat ?? [];
    const details: string[] = [];
    const uploads: string[] = [];
    const log: string[] = [];
    const left: string[] = [];
    const capture = { on: true, releases: 0, holds: 0, video: [] as string[] };
    let now = Date.parse('2026-11-02T10:00:00.000Z');
    const deps: ConsentFlowDeps = {
        clock: () => now,
        markJoined: async () => {
            log.push('markJoined');
        },
        capture: {
            isOn: () => capture.on,
            hold: () => {
                capture.on = false;
                capture.holds += 1;
                log.push('hold');
            },
            release: () => {
                capture.on = true;
                capture.releases += 1;
                log.push('release');
            },
            startVideo: async () => {
                capture.video.push('start');
            },
            stopVideo: async () => {
                capture.video.push('stop');
                await options.stopVideo?.();
            },
        },
        api: () => api,
        setStatusDetail: async (detail) => {
            details.push(detail ?? '(cleared)');
        },
        createMeeting: async () => {
            log.push('createMeeting');
        },
        upload: async (key) => {
            uploads.push(key);
        },
        callId: () => 'Weekly sync-2026-11-02',
        lmaUser: 'Ana Silva',
        meetingName: 'Weekly sync',
        ownIdentities: ['LMA (Ana Silva)', 'LMA'],
        startMessages: () => ['Live Meeting Assistant started.'],
        exitMessages: () => ['Live Meeting Assistant has left the room.'],
        sleep: async () => {
            options.onSleep?.();
        },
        startRetryMs: 0,
        noticeRepostMs: 0,
    };
    const flow = new MeetingConsentFlow(deps);
    await flow.prepare(
        async () => options.choice ?? null,
        async () => options.deployment ?? { mode: 'notify' },
    );
    capture.on = !flow.held;
    flow.attach({
        sendMessages: async (messages) => {
            chat.push(...messages);
        },
        screenshots:
            options.screenshots === false
                ? undefined
                : async () => [
                      { view: 'chat', image: Buffer.from('chat'), capturedAt: '2026-11-02T10:01:00.000Z' },
                      { view: 'participants', image: Buffer.from('list'), capturedAt: '2026-11-02T10:01:01.000Z' },
                  ],
        readRoster: options.readRoster,
        sendersHidden: options.sendersHidden,
        leave: (name) => left.push(name),
    });
    const settle = async () => {
        await flow.begun;
        await flow.evidenceSettled();
        await flow.evidenceSettled();
        await flow.videoSettled();
    };
    await settle();
    return { flow, api, chat, details, uploads, log, capture, left, settle, advance: (ms) => (now += ms) };
}

// ─── Planning ───────────────────────────────────────────────────────────────

test('the meeting runs in the deployment mode unless it chose another the deployment allows', () => {
    const notify = effectiveConsentSettings({ mode: 'notify' });
    assert.equal(planConsent(null, notify).mode, 'notify');
    assert.deepEqual(planConsent({ mode: 'opt_in', rule: 'owner_attests' }, notify).requested, { Mode: 'opt_in', Rule: 'owner_attests' });
    const strict = effectiveConsentSettings({ mode: 'opt_in', consentRule: 'each_participant', allowPerMeetingOverride: false });
    const lessStrict = planConsent({ mode: 'notify' }, strict);
    assert.equal(lessStrict.mode, 'opt_in', 'a less strict choice is not allowed');
    assert.deepEqual(lessStrict.requested, {});
    assert.equal(planConsent({ mode: 'off' }, effectiveConsentSettings({ mode: 'notify' })).mode, 'off', 'allowed by default');
});

test('settings that cannot be read leave the mode unknown unless the meeting chose opt-in', () => {
    assert.equal(planConsent(null, null).mode, 'unknown');
    assert.equal(planConsent({ mode: 'off' }, null).mode, 'unknown');
    assert.equal(planConsent({ mode: 'opt_in', rule: 'owner_attests' }, null).mode, 'opt_in');
});

test('a section value of the wrong type is the default', () => {
    const settings = effectiveConsentSettings({ mode: 'sometimes', consentRule: 7, consentPhrases: '', leaveOnObjection: 'yes' });
    assert.equal(settings.mode, 'off');
    assert.equal(settings.rule, 'each_participant');
    assert.deepEqual(settings.consentPhrases, ['I consent', 'I agree', 'I accept']);
    assert.equal(settings.leaveOnObjection, false);
});

// ─── Off ────────────────────────────────────────────────────────────────────

test('mode off runs no flow: nothing is posted, nothing is called, capture is untouched', async () => {
    const h = await harness({ deployment: { mode: 'off' } });
    assert.equal(h.flow.active, false);
    assert.deepEqual(h.chat, []);
    assert.deepEqual(h.api.calls, []);
    assert.equal(h.capture.holds, 0);
    assert.equal(await h.flow.handleChat('Bo Chen', 'I object'), false);
});

test('an unreadable setting that turns out to be off releases capture once the API answers', async () => {
    const events: string[] = [];
    const api = new FakeApi({ mode: 'off', rule: 'each_participant' });
    const flow = new MeetingConsentFlow({
        capture: {
            isOn: () => false,
            hold: () => events.push('hold'),
            release: () => events.push('release'),
            startVideo: async () => {
                events.push('startVideo');
            },
            stopVideo: async () => {},
        },
        api: () => api,
        setStatusDetail: async () => {},
        createMeeting: async () => {
            events.push('createMeeting');
        },
        upload: async () => {},
        callId: () => 'c',
        lmaUser: 'Ana',
        meetingName: 'm',
        ownIdentities: [],
        startMessages: () => ['started'],
        exitMessages: () => [],
        sleep: async () => {},
    });
    const result = await flow.prepare(
        async () => null,
        async () => {
            throw new Error('table unreadable');
        },
    );
    assert.deepEqual(result, { holdCapture: true, deferVideo: true });
    assert.equal(flow.plan.mode, 'unknown');
    flow.attach({ sendMessages: async () => {}, leave: () => {} });
    await flow.begun;
    assert.deepEqual(events, ['hold', 'createMeeting', 'release']);
    assert.equal(flow.active, false);
});

test("a meeting choice that cannot be read withholds capture until it is read, then is used", async () => {
    const events: string[] = [];
    const api = new FakeApi({ mode: 'notify', rule: 'each_participant' });
    let readable = false;
    const flow = new MeetingConsentFlow({
        capture: {
            isOn: () => false,
            hold: () => events.push('hold'),
            release: () => events.push('release'),
            startVideo: async () => {},
            stopVideo: async () => {},
        },
        api: () => api,
        setStatusDetail: async () => {},
        createMeeting: async () => {},
        upload: async () => {},
        callId: () => 'c',
        lmaUser: 'Ana',
        meetingName: 'm',
        ownIdentities: [],
        startMessages: () => ['started'],
        exitMessages: () => [],
        sleep: async () => {},
        startAttempts: 2,
    });
    const readChoice = async () => {
        if (!readable) throw new Error('AppSync unavailable');
        return { mode: 'opt_in', rule: 'owner_attests' };
    };
    const result = await flow.prepare(readChoice, async () => ({ mode: 'notify' }));
    assert.equal(flow.plan.mode, 'unknown', 'not the deployment notify: the meeting may have chosen opt-in');
    assert.deepEqual(result, { holdCapture: true, deferVideo: true });
    flow.attach({ sendMessages: async () => {}, leave: () => {} });
    await flow.begun;
    assert.deepEqual(api.starts, [], 'consent is not started with a choice that may be wrong');
    readable = true;
    await flow.pollOnce();
    assert.deepEqual(api.starts[0], { Mode: 'opt_in', Rule: 'owner_attests', LmaUser: 'Ana', MeetingName: 'm' });
    assert.equal(api.record?.Mode, 'opt_in');
    assert.deepEqual(events, ['hold'], 'still withheld: the owner has not attested');
    flow.stop();
});

// ─── Notify ─────────────────────────────────────────────────────────────────

test('notify: capture runs from the start, the notice is posted and recorded, and evidence is kept', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    assert.equal(h.capture.holds, 0);
    assert.equal(h.capture.on, true);
    assert.ok(h.chat.some((m) => m.includes('Reply "I object"')), 'the notice is in the chat');
    assert.ok(h.api.calls.includes('recordNotice'));
    assert.equal(h.api.record?.Status, 'granted');
    assert.equal(h.uploads.length, 2, 'chat and participant list');
    for (const key of h.uploads) assert.match(key, /^lma-consent-evidence\/Weekly_sync_2026_11_02\/.+-(chat|participants)-[0-9a-f]{6}\.jpg$/);
    assert.equal(h.api.verified.length, 1, 'only the chat screenshot is checked');
});

test('notify: an objection stops capture at once, is recorded, acknowledged and evidenced', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    const before = h.uploads.length;
    assert.equal(await h.flow.handleChat('Bo Chen', 'I object'), true);
    await h.settle();
    assert.equal(h.capture.on, false);
    assert.ok(h.log.includes('hold'));
    assert.ok(h.api.calls.includes('recordEvent:Bo Chen:objection'));
    assert.ok(h.chat.some((m) => m.startsWith('Bo Chen objected.')));
    assert.equal(h.uploads.length, before + 2);
    assert.deepEqual(h.capture.video, ['stop'], 'the screen recording stops for the rest of the meeting');
    assert.equal(h.api.record?.Status, 'objected');
    assert.match(h.details[h.details.length - 1], /^Not recording/);
    assert.deepEqual(h.left, [], 'stays by default');
});

test('an objection is acknowledged, recorded and evidenced without waiting for the screen recording to be finalised', async () => {
    let finishVideo: () => void = () => {};
    const videoDone = new Promise<void>((resolve) => {
        finishVideo = resolve;
    });
    const h = await harness({ deployment: { mode: 'notify' }, stopVideo: () => videoDone });
    const before = h.uploads.length;
    await h.flow.handleChat('Bo Chen', 'I object');
    await h.flow.evidenceSettled();
    assert.equal(h.capture.on, false);
    assert.ok(h.chat.some((m) => m.startsWith('Bo Chen objected.')));
    assert.ok(h.api.calls.includes('recordEvent:Bo Chen:objection'));
    assert.equal(h.uploads.length, before + 2);
    assert.deepEqual(h.capture.video, ['stop'], 'begun, still finalising');
    finishVideo();
    await h.flow.videoSettled();
});

test('a record answer already on its way when an objection is read does not release capture', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    await h.flow.handleChat('Bo Chen', 'I object');
    await h.settle();
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.settle();
    assert.equal(h.capture.on, true);
    const releases = h.capture.releases;
    // A read that starts now answers from the record as it is now: allowed.
    const get = h.api.get.bind(h.api);
    let answer: () => void = () => {};
    const held = new Promise<void>((resolve) => {
        answer = resolve;
    });
    h.api.get = async () => {
        const state = await get();
        await held;
        return state;
    };
    const poll = h.flow.pollOnce();
    await new Promise((resolve) => setTimeout(resolve, 10));
    h.advance(10_000);
    const objection = h.flow.handleChat('Cy Diaz', 'I object');
    await new Promise((resolve) => setTimeout(resolve, 10));
    answer();
    await poll;
    await objection;
    await h.settle();
    assert.equal(h.capture.releases, releases, 'the earlier answer did not release capture');
    assert.equal(h.capture.on, false);
    assert.equal(h.api.record?.Status, 'objected');
});

test('a poll does not start while the previous one is still running', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    const get = h.api.get.bind(h.api);
    let release: () => void = () => {};
    const slow = new Promise<void>((resolve) => {
        release = resolve;
    });
    let gets = 0;
    h.api.get = async () => {
        gets += 1;
        await slow;
        return get();
    };
    const first = h.flow.pollOnce();
    await new Promise((resolve) => setTimeout(resolve, 10));
    await h.flow.pollOnce();
    await h.flow.pollOnce();
    release();
    await first;
    assert.equal(gets, 1);
});

test('notify: the objector consenting later resumes capture', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    await h.flow.handleChat('Bo Chen', 'I object');
    await h.settle();
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.settle();
    assert.equal(h.capture.on, true);
    assert.equal(h.capture.releases, 1);
    assert.ok(h.chat.includes('Live Meeting Assistant started.'));
    assert.equal(h.details[h.details.length - 1], '(cleared)');
});

test('notify with leaveOnObjection: the VP says goodbye and leaves after an objection', async () => {
    const h = await harness({ deployment: { mode: 'notify', leaveOnObjection: true } });
    await h.flow.handleChat('Bo Chen', 'stop recording');
    await h.settle();
    assert.deepEqual(h.left, ['Bo Chen']);
    assert.ok(h.chat.some((m) => m.includes('It will now leave the meeting.')));
    assert.ok(h.chat.includes('Live Meeting Assistant has left the room.'));
});

test('an objection entered on the meeting page stops capture and is acknowledged', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    h.api.objectFromPage('Cy Diaz');
    await h.flow.pollOnce();
    await h.settle();
    assert.equal(h.capture.on, false);
    assert.ok(h.chat.some((m) => m.startsWith('An objection for Cy Diaz was recorded in LMA.')));
});

test('a consent in the chat after an objection recorded in LMA withdraws it', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.settle();
    h.api.objectFromPage('Bo Chen');
    await h.flow.pollOnce();
    await h.settle();
    assert.equal(h.capture.on, false);
    h.advance(10_000);
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.settle();
    assert.equal(h.api.calls.filter((c) => c === 'recordEvent:Bo Chen:consent').length, 2);
    assert.equal(h.capture.on, true);
});

test('an objection with no sender still stops capture; consent with no sender is not recorded', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    assert.equal(await h.flow.handleChat(null, 'I consent'), true);
    assert.ok(!h.api.calls.some((c) => c.startsWith('recordEvent')));
    assert.ok(!h.chat.some((m) => m.includes('could not tell who sent that reply')), 'nothing to say in notify');
    await h.flow.handleChat(null, 'I object');
    await h.settle();
    assert.equal(h.capture.on, false);
    const ack = h.chat.find((m) => m.startsWith('Unidentified participant objected.'));
    assert.ok(ack && !ack.includes('can continue'), 'no way to withdraw is offered to a sender nobody can name');
    assert.ok(h.api.calls.includes(`recordEvent:${'Unidentified participant'}:objection`));
});

test('a reply repeated by a chat observer is acted on once', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    await h.flow.handleChat('Bo Chen', 'I object');
    await h.flow.handleChat('Bo Chen', 'I object');
    await h.settle();
    assert.equal(h.api.calls.filter((c) => c === 'recordEvent:Bo Chen:objection').length, 1);
});

test('the VP never reads its own messages as replies', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    for (const message of h.chat) assert.equal(await h.flow.handleChat(null, message), false, message);
    assert.equal(await h.flow.handleChat('LMA (Ana Silva)', 'I object'), false);
    assert.equal(h.capture.on, true);
});

// ─── Opt-in, each participant ───────────────────────────────────────────────

const OPT_IN_EACH = { mode: 'opt_in', consentRule: 'each_participant' };

test('opt-in: capture is withheld before joining and the meeting record is created for its consent', async () => {
    const h = await harness({ deployment: OPT_IN_EACH });
    assert.equal(h.flow.held, true);
    assert.equal(h.capture.on, false);
    assert.deepEqual(h.log.slice(0, 3), ['hold', 'markJoined', 'createMeeting']);
    assert.equal(h.details[0], 'Waiting for consent…');
    assert.ok(h.chat.some((m) => m.includes('Reply "I consent"')));
    assert.deepEqual(h.uploads, [], 'nothing is granted yet');
});

test('each participant: capture starts only when everyone present has consented', async () => {
    const h = await harness({ deployment: OPT_IN_EACH });
    h.flow.noteAttendeeCount(3); // the VP and two others
    noteParticipantSeen('Bo Chen');
    await h.flow.syncRoster();
    assert.deepEqual(h.api.record?.Participants, ['Bo Chen', `${UNIDENTIFIED_PARTICIPANT} 1`]);

    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.settle();
    assert.equal(h.capture.on, false, 'one of two has consented');
    assert.ok(h.chat.some((m) => m === 'Thank you, Bo Chen. Still waiting for 1 unidentified participant.'));
    assert.match(h.details[h.details.length - 1], /^Waiting for consent: 1 of 2 participants present have not consented yet\.$/);

    await h.flow.handleChat('Cy Diaz', 'LMA consent');
    await h.flow.syncRoster();
    await h.settle();
    assert.equal(h.capture.on, true);
    assert.equal(h.capture.releases, 1);
    assert.deepEqual(h.capture.video, ['start'], 'the screen recording starts with consent');
    assert.ok(h.chat.includes('Everyone present has consented.'));
    assert.ok(h.chat.includes('Live Meeting Assistant started.'));
    assert.equal(h.uploads.length, 2, 'evidence when consent was granted');
    assert.equal(h.details[h.details.length - 1], '(cleared)');
});

test('each participant: somebody joining while recording pauses it and the notice is posted again', async () => {
    const h = await harness({ deployment: OPT_IN_EACH });
    h.flow.noteAttendeeCount(2);
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.flow.syncRoster();
    await h.settle();
    assert.equal(h.capture.on, true);
    const notices = h.api.calls.filter((c) => c === 'recordNotice').length;

    h.flow.noteAttendeeCount(3);
    await h.flow.syncRoster();
    await h.settle();
    assert.equal(h.capture.on, false, 'paused until the newcomer consents');
    assert.deepEqual(h.capture.video, ['start', 'stop']);
    // The repost waits a few seconds after the join; give it the chance.
    await new Promise((resolve) => setTimeout(resolve, 3100));
    await h.settle();
    assert.equal(h.api.calls.filter((c) => c === 'recordNotice').length, notices + 1);
    assert.ok(h.chat.includes('Recording and transcription are paused until everyone present has consented.'));

    await h.flow.handleChat('Dee Evans', 'I consent');
    noteParticipantSeen('Bo Chen'); // seen again since the join
    await h.flow.syncRoster();
    await h.settle();
    assert.equal(h.capture.on, true);
    h.flow.stop();
});

test('"LMA start" is refused while consent is withheld', async () => {
    const h = await harness({ deployment: OPT_IN_EACH });
    assert.equal(await h.flow.handleChat('Bo Chen', 'LMA start'), true);
    assert.ok(h.chat.includes('Live Meeting Assistant cannot start until consent is given.'));
    assert.equal(h.capture.on, false);
});

test('a participant who paused from the chat is not resumed by consent', async () => {
    const h = await harness({ deployment: OPT_IN_EACH });
    h.flow.noteAttendeeCount(2);
    assert.equal(await h.flow.handleChat('Bo Chen', 'LMA pause'), false, 'the handler still answers the command');
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.flow.syncRoster();
    await h.settle();
    assert.equal(h.capture.releases, 0);
    assert.ok(h.chat.some((m) => m.startsWith('Transcription stays paused')));
});

test('with no attendee count and no roster, each-participant consent is never granted from the chat', async () => {
    const h = await harness({ deployment: OPT_IN_EACH });
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.flow.syncRoster();
    await h.settle();
    assert.equal(h.capture.on, false);
    assert.doesNotMatch(h.details[h.details.length - 1], /does not show LMA/, 'not said before a count could arrive');
    h.advance(61_000);
    await h.flow.pollOnce();
    await h.flow.handleChat('Cy Diaz', 'I consent');
    await h.settle();
    assert.equal(h.capture.on, false);
    assert.match(h.details[h.details.length - 1], /does not show LMA how many people are present/);
});

test('an objection that could not be recorded keeps capture withheld until it is', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    const recordEvent = h.api.recordEvent.bind(h.api);
    let failures = 2;
    h.api.recordEvent = async (input) => {
        if (failures > 0) {
            failures -= 1;
            throw new Error('throttled');
        }
        return recordEvent(input);
    };
    await h.flow.handleChat('Bo Chen', 'I object');
    await h.settle();
    assert.equal(h.capture.on, false);
    // The record does not show the objection yet and says capture is allowed:
    // it is not released on that answer.
    await h.flow.pollOnce();
    await h.settle();
    assert.equal(h.capture.on, false);
    assert.equal(h.capture.releases, 0);
    await h.flow.pollOnce();
    await h.settle();
    assert.equal(h.api.record?.Status, 'objected');
    assert.equal(h.capture.on, false);
});

test('where the platform shows no chat sender, the status line says why each-participant consent cannot be met', async () => {
    const h = await harness({ deployment: OPT_IN_EACH, sendersHidden: true });
    h.flow.noteAttendeeCount(3);
    await h.flow.handleChat(null, 'I consent');
    assert.ok(h.chat.some((m) => m.includes('could not tell who sent that reply')), 'said where consent is awaited');
    await h.flow.syncRoster();
    await h.settle();
    assert.equal(h.capture.on, false);
    assert.match(h.details[h.details.length - 1], /does not show LMA who sent a chat message/);
});

test("a platform's own participant list is used where it accounts for everybody", async () => {
    let names = ['Bo Chen'];
    const h = await harness({ deployment: OPT_IN_EACH, readRoster: async () => names });
    h.flow.noteAttendeeCount(2);
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.flow.syncRoster();
    await h.settle();
    assert.equal(h.capture.on, true);
    names = ['Bo Chen', 'Cy Diaz'];
    h.flow.noteAttendeeCount(3);
    await h.flow.syncRoster();
    await h.settle();
    assert.deepEqual(h.api.record?.Participants, ['Bo Chen', 'Cy Diaz']);
    assert.equal(h.capture.on, false);
    h.flow.stop();
});

// ─── Opt-in, owner attests ──────────────────────────────────────────────────

test('owner attests: capture waits for the attestation made on the meeting page', async () => {
    const h = await harness({ deployment: { mode: 'opt_in', consentRule: 'owner_attests' } });
    assert.ok(h.chat.includes('Recording starts when Ana Silva confirms in LMA that everyone has agreed.'));
    await h.flow.handleChat('Bo Chen', 'I consent');
    await h.settle();
    assert.equal(h.capture.on, false, 'a participant reply is recorded but is not the attestation');
    h.api.attest();
    await h.flow.pollOnce();
    await h.settle();
    assert.equal(h.capture.on, true);
    assert.ok(h.chat.includes('Ana Silva confirmed in LMA that everyone has agreed.'));
    assert.equal(h.uploads.length, 2);
});

test('owner attests: an objection still stops capture after the attestation', async () => {
    const h = await harness({ deployment: { mode: 'opt_in', consentRule: 'owner_attests' } });
    h.api.attest();
    await h.flow.pollOnce();
    await h.settle();
    await h.flow.handleChat('Bo Chen', 'I do not consent');
    await h.settle();
    assert.equal(h.capture.on, false);
});

// ─── The meeting's own choice, start retries, evidence budget ───────────────

test("the meeting's own choice is asked for, and the deployment's used when it is refused", async () => {
    const api = new FakeApi({ mode: 'notify', rule: 'each_participant' });
    api.refuseChoice = true;
    const h = await harness({ deployment: { mode: 'notify' }, choice: { mode: 'opt_in', rule: 'owner_attests' }, api });
    assert.deepEqual(h.api.starts[0].Mode, 'opt_in');
    assert.equal(h.api.starts[1].Mode, undefined);
    assert.equal(h.api.record?.Mode, 'notify');
    // The VP held capture for opt-in; the meeting turned out notify, so it runs.
    assert.equal(h.capture.on, true);
});

test("a temporary refusal keeps the meeting's own choice", async () => {
    const api = new FakeApi({ mode: 'off', rule: 'each_participant' });
    const start = api.start.bind(api);
    let busy = 1;
    api.start = async (input) => {
        if (busy > 0) {
            busy -= 1;
            api.starts.push(input);
            throw new ConsentApiError("the meeting's consent kept changing; try again", 'Lambda:Unhandled');
        }
        return start(input);
    };
    const h = await harness({ deployment: { mode: 'off' }, choice: { mode: 'opt_in', rule: 'owner_attests' }, api });
    assert.equal(h.api.starts.length, 2);
    assert.equal(h.api.starts[1].Mode, 'opt_in', 'asked for again, not dropped');
    assert.equal(h.api.record?.Mode, 'opt_in');
    assert.equal(h.capture.on, false);
});

test('in notify, the notice is posted after the first failed start, not after the last', async () => {
    const api = new FakeApi({ mode: 'notify', rule: 'each_participant' });
    api.notCreatedFor = 1000;
    const chat: string[] = [];
    const noticeAtSleep: boolean[] = [];
    const h = await harness({
        deployment: { mode: 'notify' },
        api,
        chat,
        onSleep: () => noticeAtSleep.push(chat.some((m) => m.includes('If you do not agree'))),
    });
    assert.equal(noticeAtSleep[0], true, 'already posted when the first retry waits');
    assert.equal(h.chat.filter((m) => m.includes('If you do not agree')).length, 1, 'posted once');
});

test('a meeting that chose Off in an opt-in deployment captures as usual and says so on its record', async () => {
    const h = await harness({ deployment: OPT_IN_EACH, choice: { mode: 'off' } });
    assert.equal(h.capture.holds, 0);
    assert.equal(h.capture.on, true);
    assert.deepEqual(h.api.starts.map((s) => s.Mode), ['off']);
    assert.equal(h.api.record?.Mode, 'off');
    assert.deepEqual(h.chat, [], 'no notice');
    assert.equal(h.flow.active, false);
    assert.equal(await h.flow.handleChat('Bo Chen', 'I object'), false);
});

test('a meeting that chose Off, read only after joining, still goes on its record, and capture is released', async () => {
    const events: string[] = [];
    const api = new FakeApi({ mode: 'opt_in', rule: 'each_participant' });
    let readable = false;
    let on = true;
    const flow = new MeetingConsentFlow({
        capture: {
            isOn: () => on,
            hold: () => {
                on = false;
                events.push('hold');
            },
            release: () => {
                on = true;
                events.push('release');
            },
            startVideo: async () => {},
            stopVideo: async () => {},
        },
        api: () => api,
        setStatusDetail: async () => {},
        createMeeting: async () => {},
        upload: async () => {},
        callId: () => 'c',
        lmaUser: 'Ana',
        meetingName: 'm',
        ownIdentities: [],
        startMessages: () => ['started'],
        exitMessages: () => [],
        sleep: async () => {},
        startAttempts: 1,
    });
    await flow.prepare(
        async () => {
            if (!readable) throw new Error('AppSync unavailable');
            return { mode: 'off' };
        },
        async () => ({ mode: 'opt_in', consentRule: 'each_participant' }),
    );
    flow.attach({ sendMessages: async () => {}, leave: () => {} });
    await flow.begun;
    assert.deepEqual(events, ['hold']);
    readable = true;
    await flow.pollOnce();
    assert.deepEqual(api.starts.map((s) => s.Mode), ['off']);
    assert.equal(api.record?.Mode, 'off');
    assert.deepEqual(events, ['hold', 'release']);
    assert.equal(flow.active, false);
});

test('a participant shown as "Unknown" can still object', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    assert.equal(await h.flow.handleChat('Unknown', 'I object'), true);
    await h.settle();
    assert.equal(h.capture.on, false);
    assert.ok(h.api.calls.includes('recordEvent:Unidentified participant:objection'));
});

test('a meeting that chose Off, refused by the resolver, runs the deployment mode', async () => {
    const api = new FakeApi({ mode: 'opt_in', rule: 'each_participant' });
    api.refuseChoice = true;
    const h = await harness({ deployment: OPT_IN_EACH, choice: { mode: 'off' }, api });
    assert.deepEqual(h.api.starts.map((s) => s.Mode), ['off', undefined]);
    assert.equal(h.api.record?.Mode, 'opt_in');
    assert.equal(h.capture.on, false, 'withheld, as the deployment requires');
    assert.ok(h.chat.some((m) => m.includes('Reply "I consent"')));
});

test('consent is started once the meeting record exists', async () => {
    const api = new FakeApi({ mode: 'opt_in', rule: 'each_participant' });
    api.notCreatedFor = 3;
    const h = await harness({ deployment: OPT_IN_EACH, api });
    assert.equal(h.api.starts.length, 4);
    assert.ok(h.api.calls.includes('recordNotice'));
});

test('replies before consent has started are recorded once it has', async () => {
    const api = new FakeApi({ mode: 'notify', rule: 'each_participant' });
    api.notCreatedFor = 1000;
    const h = await harness({ deployment: { mode: 'notify' }, api });
    assert.ok(h.chat.some((m) => m.includes('If you do not agree')), 'the notice is posted even before the record can be written');
    await h.flow.handleChat('Bo Chen', 'I object');
    assert.equal(h.capture.on, false, 'and an objection stops capture at once');
    api.notCreatedFor = 0;
    await h.flow.pollOnce();
    await h.settle();
    assert.ok(api.calls.includes('recordEvent:Bo Chen:objection'));
    assert.equal(api.record?.Status, 'objected');
});

test('a notice posted before consent could start is recorded as it was posted', async () => {
    const api = new FakeApi({ mode: 'opt_in', rule: 'owner_attests' });
    api.notCreatedFor = 1000;
    const h = await harness({ deployment: { mode: 'opt_in', consentRule: 'owner_attests' }, api });
    const posted = h.chat.filter((m) => !m.startsWith('Live Meeting Assistant started')).join(' ');
    assert.ok(posted.includes('confirms in LMA'), 'the owner-attests line was posted');
    api.notCreatedFor = 0;
    await h.flow.pollOnce();
    await h.settle();
    assert.equal(api.noticeTexts.length, 1);
    assert.equal(api.noticeTexts[0], posted);
});

test('evidence stops at the per-meeting budget', async () => {
    const h = await harness({ deployment: { mode: 'notify' } });
    for (let i = 0; i < 20; i += 1) {
        await h.flow.handleChat(`P${i}`, 'I object');
        await h.flow.handleChat(`P${i}`, 'I consent');
    }
    await h.settle();
    assert.equal(h.uploads.length, MAX_EVIDENCE_IMAGES);
});

test('describeWaiting names people and counts unidentified seats', () => {
    assert.equal(describeWaiting(['Bo Chen']), 'Bo Chen');
    assert.equal(describeWaiting(['Bo Chen', 'Cy Diaz', `${UNIDENTIFIED_PARTICIPANT} 1`, `${UNIDENTIFIED_PARTICIPANT} 2`]), 'Bo Chen, Cy Diaz and 2 unidentified participants');
});
