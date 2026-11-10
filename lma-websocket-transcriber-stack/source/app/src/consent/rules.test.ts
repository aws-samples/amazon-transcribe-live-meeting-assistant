/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the transcriber decides about a meeting's consent before there is a record.
 *
 * - The settings and their defaults are lma_consent's and the seeder's.
 * - Off captures; notify captures from the start; opt-in captures only once the
 *   owner's confirmation is recorded, and refuses a meeting without one.
 * - A client with no consent step is captured with a warning, or refused, as the
 *   deployment chooses; the default keeps clients without the step working.
 * - Evidence images are JPEG or PNG by their bytes, within the checker's limit,
 *   and stored under the folder the resolver accepts evidence from.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import {
    CLIENTS_REJECT,
    DEFAULT_SETTINGS,
    MAX_EVIDENCE_BYTES,
    decideAtStart,
    decodeEvidenceImage,
    defaultStatement,
    effectiveSettings,
    evidenceKey,
    noticeChannelFor,
    parseClientConsent,
    renderNotice,
    ruleForCaptureClients,
    settingsFrame,
} from './rules';

const REPO = path.resolve(__dirname, '../../../../..');

test('the defaults are the seeder defaults', () => {
    const seeded = JSON.parse(
        fs.readFileSync(
            path.join(REPO, 'lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/MeetingConsent.json'),
            'utf8'
        )
    ) as Record<string, unknown>;
    for (const [key, value] of Object.entries(DEFAULT_SETTINGS)) {
        assert.deepEqual(seeded[key], value, `${key} differs from the seeder's MeetingConsent.json`);
    }
});

test('the client policy is a setting of the Meeting Consent section', () => {
    const schema = JSON.parse(
        fs.readFileSync(
            path.join(REPO, 'lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/schemas/meeting_consent.json'),
            'utf8'
        )
    ) as { properties: Record<string, { enum?: string[] }> };
    assert.deepEqual(schema.properties['clientsWithoutConsent']?.enum, ['accept_with_warning', 'reject']);
});

test('a value of the wrong type, or an unknown mode, rule or policy, gives way to the default', () => {
    const settings = effectiveSettings({
        mode: 'sometimes',
        consentRule: 7,
        allowPerMeetingOverride: 'yes',
        clientsWithoutConsent: 'ignore',
        consentPhrases: 'yes, sure ,',
    });
    assert.equal(settings.mode, 'off');
    assert.equal(settings.consentRule, 'each_participant');
    assert.equal(settings.allowPerMeetingOverride, true);
    assert.equal(settings.clientsWithoutConsent, 'accept_with_warning');
    assert.deepEqual(settings.consentPhrases, ['yes', 'sure']);
});

test('a notice is filled as lma_consent fills it', () => {
    assert.equal(
        renderNotice('{LMA_USER} in {meeting}: say {consent_phrase} or {objection_phrase}; {other}', {
            lmaUser: 'Ana',
            meeting: 'Sync',
            consentPhrase: 'I agree',
            objectionPhrase: 'I object',
        }),
        'Ana in Sync: say I agree or I object; {other}'
    );
    assert.equal(renderNotice('{LMA_USER} / {meeting}', {}), 'The meeting organizer / this meeting');
    assert.equal(renderNotice('x'.repeat(3000), {}).length, 2000);
});

test('opt-in is met by the owner attesting, unless the deployment insists on each participant', () => {
    const optIn = (consentRule: string, allowPerMeetingOverride: boolean) =>
        effectiveSettings({ mode: 'opt_in', consentRule, allowPerMeetingOverride });
    assert.deepEqual(ruleForCaptureClients(optIn('owner_attests', false)), { supported: true, rule: 'owner_attests' });
    assert.deepEqual(ruleForCaptureClients(optIn('each_participant', true)), { supported: true, rule: 'owner_attests' });
    const refused = ruleForCaptureClients(optIn('each_participant', false));
    assert.equal(refused.supported, false);
    assert.match(refused.reason ?? '', /Virtual Participant/);
    assert.deepEqual(ruleForCaptureClients(effectiveSettings({ mode: 'notify' })), { supported: true, rule: null });
});

test('the settings frame carries the notice for the mode in force', () => {
    const frame = settingsFrame(effectiveSettings({ mode: 'notify' }));
    assert.equal(frame['event'], 'CONSENT_SETTINGS');
    assert.equal(frame['mode'], 'notify');
    assert.equal(frame['noticeTemplate'], DEFAULT_SETTINGS['notifyNoticeText']);
    assert.equal(frame['objectionPhrase'], 'I object');
    assert.equal(settingsFrame(effectiveSettings({}))['noticeTemplate'], '');
});

test('a consent step is read field by field, and anything unknown confirms nothing', () => {
    assert.equal(parseClientConsent(undefined), undefined);
    assert.equal(parseClientConsent('yes'), undefined);
    assert.equal(parseClientConsent([1]), undefined);
    const parsed = parseClientConsent({ mode: 'opt_in', confirmed: 'true', method: 'telepathy', lmaUser: ' Ana ' });
    assert.equal(parsed?.mode, 'opt_in');
    assert.equal(parsed?.confirmed, false);
    assert.equal(parsed?.method, undefined);
    assert.equal(parsed?.lmaUser, 'Ana');
    assert.equal(parseClientConsent({ mode: 'always' })?.mode, 'off');
});

const confirmedOptIn = parseClientConsent({ mode: 'opt_in', confirmed: true, method: 'verbal' });

test('off captures whatever the client sent', () => {
    assert.deepEqual(decideAtStart(effectiveSettings({}), undefined), { kind: 'capture' });
    assert.deepEqual(decideAtStart(effectiveSettings({}), confirmedOptIn), { kind: 'capture' });
});

test('notify captures from the start, confirmed or not', () => {
    const notify = effectiveSettings({ mode: 'notify' });
    const unconfirmed = parseClientConsent({ mode: 'notify', confirmed: false });
    assert.deepEqual(decideAtStart(notify, unconfirmed), { kind: 'consent', mode: 'notify', rule: null, gateOpen: true });
});

test('opt-in waits for the record when the owner confirmed, and refuses the meeting otherwise', () => {
    const optIn = effectiveSettings({ mode: 'opt_in' });
    assert.deepEqual(decideAtStart(optIn, confirmedOptIn), {
        kind: 'consent',
        mode: 'opt_in',
        rule: 'owner_attests',
        gateOpen: false,
    });
    const unconfirmed = decideAtStart(optIn, parseClientConsent({ mode: 'opt_in', confirmed: true }));
    assert.equal(unconfirmed.kind, 'reject');
    assert.equal(unconfirmed.kind === 'reject' && unconfirmed.code, 'CONSENT_NOT_CONFIRMED');
    // A step shown for notify does not confirm agreement.
    const stale = decideAtStart(optIn, parseClientConsent({ mode: 'notify', confirmed: true, method: 'chat' }));
    assert.equal(stale.kind === 'reject' && stale.code, 'CONSENT_NOT_CONFIRMED');
    const strict = decideAtStart(
        effectiveSettings({ mode: 'opt_in', allowPerMeetingOverride: false }),
        confirmedOptIn
    );
    assert.equal(strict.kind === 'reject' && strict.code, 'CONSENT_RULE_UNSUPPORTED');
});

test('a client without a consent step is captured with a warning by default, and refused when chosen', () => {
    const accepted = decideAtStart(effectiveSettings({ mode: 'opt_in' }), undefined);
    assert.equal(accepted.kind, 'capture');
    assert.match(accepted.kind === 'capture' ? accepted.warning ?? '' : '', /no consent step/);
    const refused = decideAtStart(
        effectiveSettings({ mode: 'notify', clientsWithoutConsent: CLIENTS_REJECT }),
        undefined
    );
    assert.equal(refused.kind === 'reject' && refused.code, 'CONSENT_REQUIRED');
});

test('unreadable settings record the step in the mode it was shown for, and capture a client without one', () => {
    assert.deepEqual(decideAtStart(undefined, confirmedOptIn), {
        kind: 'consent',
        mode: 'opt_in',
        rule: 'owner_attests',
        gateOpen: false,
        requireRecord: true,
    });
    // Even a notify step waits for the record: the resolver decides.
    assert.deepEqual(decideAtStart(undefined, parseClientConsent({ mode: 'notify', confirmed: true, method: 'chat' })), {
        kind: 'consent',
        mode: 'notify',
        rule: null,
        gateOpen: false,
        requireRecord: true,
    });
    assert.equal(decideAtStart(undefined, undefined).kind, 'capture');
});

test('the notice channel and the default statement follow the method', () => {
    assert.equal(noticeChannelFor('verbal'), 'voice');
    assert.equal(noticeChannelFor('chat'), 'chat');
    assert.equal(noticeChannelFor('invite_terms'), 'invite');
    assert.match(defaultStatement('verbal', 'Stream Audio'), /in Stream Audio .*out loud/);
    assert.match(defaultStatement('invite_terms', undefined), /invitation/);
});

const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]);
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00]);

test('an evidence image is a JPEG or PNG by its bytes, within the checker limit', () => {
    const jpeg = decodeEvidenceImage(JPEG.toString('base64'));
    assert.ok(!('error' in jpeg) && jpeg.contentType === 'image/jpeg' && jpeg.extension === 'jpg');
    const dataUrl = decodeEvidenceImage(`data:image/png;base64,${PNG.toString('base64')}`);
    assert.ok(!('error' in dataUrl) && dataUrl.contentType === 'image/png');
    assert.ok('error' in decodeEvidenceImage(Buffer.from('GIF89a').toString('base64')));
    assert.ok('error' in decodeEvidenceImage('not base64!'));
    assert.ok('error' in decodeEvidenceImage(undefined));
    const tooLarge = Buffer.concat([JPEG, Buffer.alloc(MAX_EVIDENCE_BYTES)]).toString('base64');
    assert.ok('error' in decodeEvidenceImage(tooLarge));
});

test('evidence is stored under the folder the resolver accepts, named as it names it', () => {
    const at = new Date('2026-11-02T10:01:30.123Z');
    assert.equal(
        evidenceKey('lma-consent-evidence/', ' Stream Audio - 2026-11-02 ', at, 'ab12', 'jpg'),
        'lma-consent-evidence/Stream_Audio___2026_11_02/screenshot-20261102T100130Z-ab12.jpg'
    );
    assert.equal(evidenceKey('lma-consent-evidence/', '///', at, 'ab12', 'jpg'), undefined);
});
