/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether a Virtual Participant records the meeting's video: the deployment,
 * then the Configuration page setting, then the launch, and the first that says
 * no wins. The outcome reaches details.enableVideoRecording, which the video
 * recorder, the key-frame sampler and the consent notice's video sentence read.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    RECORD_VP_MEETING_VIDEO_DEFAULT,
    applyVideoRecordingRule,
    videoRecordingOffReason,
    videoRecordingOutcome,
} from './video-recording-rule.js';

const launch = (requested: string | undefined, deployment = 'true') => ({
    ENABLE_VIDEO_RECORDING: requested,
    DEPLOYMENT_VIDEO_RECORDING: deployment,
});

test('the shipped default is on, as setting_defaults.json has it', () => {
    const defaults = JSON.parse(
        readFileSync(
            new URL(
                '../../../lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/setting_defaults.json',
                import.meta.url,
            ).pathname,
            'utf8',
        ),
    );
    assert.equal(defaults.recording.recordVpMeetingVideo.default, RECORD_VP_MEETING_VIDEO_DEFAULT);
});

test('each combination of deployment, setting and launch', () => {
    const cases: Array<[string, string | undefined, boolean | undefined, boolean, string | null]> = [
        // deployment, launch, setting -> recorded, reason
        ['true', 'true', true, true, null],
        ['true', undefined, undefined, true, null],
        ['true', 'false', true, false, null],
        ['true', 'true', false, false, 'configuration'],
        ['true', undefined, false, false, 'configuration'],
        ['true', 'false', false, false, null],
        ['false', 'true', true, false, 'deployment'],
        ['false', 'true', false, false, 'deployment'],
        ['false', 'false', true, false, null],
    ];
    for (const [deployment, requested, setting, recorded, reason] of cases) {
        const outcome = videoRecordingOutcome(launch(requested, deployment), setting);
        const label = `deployment ${deployment}, launch ${requested}, setting ${setting}`;
        assert.equal(outcome.recorded, recorded, label);
        assert.equal(outcome.offReason, reason, label);
    }
});

test('a deployment that sets no variable (an earlier task definition) does not limit the video', () => {
    assert.equal(videoRecordingOutcome({ ENABLE_VIDEO_RECORDING: 'true' }, true).recorded, true);
});

test('applying a refusal turns the launch variable off and keeps the reason; a recorded one keeps none', () => {
    const env: Record<string, string | undefined> = { ...launch('true'), VIDEO_RECORDING_OFF_REASON: 'stale' };
    applyVideoRecordingRule(env, false);
    assert.equal(env['ENABLE_VIDEO_RECORDING'], 'false');
    assert.equal(videoRecordingOffReason(env), 'configuration');

    const recorded: Record<string, string | undefined> = { ...launch('true'), VIDEO_RECORDING_OFF_REASON: 'configuration' };
    applyVideoRecordingRule(recorded, true);
    assert.equal(recorded['ENABLE_VIDEO_RECORDING'], 'true');
    assert.equal(videoRecordingOffReason(recorded), null);
});

test('details.ts takes enableVideoRecording from the variable the rule sets, after the settings are applied', () => {
    const source = (name: string) =>
        readFileSync(new URL(`./${name}`, import.meta.url).pathname.replace('/dist/', '/src/'), 'utf8');
    const details = source('details.ts');
    assert.match(details, /enableVideoRecording: process\.env\.ENABLE_VIDEO_RECORDING !== 'false',/);
    assert.equal(
        details.split('\n').find((line) => line.startsWith('import ')),
        "import './stack-settings.js';",
    );
    assert.match(source('stack-settings.ts'), /\['recordVpMeetingVideo'\]/);
    // The consent notice's video sentence follows details.enableVideoRecording.
    assert.match(source('consent-runtime.ts'), /videoRecorded: \(\) => details\.enableVideoRecording/);
});
