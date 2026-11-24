/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether this Virtual Participant records the meeting's video.
 *
 * Three things decide it, and the first that says no wins:
 *
 * 1. The deployment: the EnableVideoRecording stack parameter, which the task
 *    definition passes as DEPLOYMENT_VIDEO_RECORDING. Its value never changes
 *    with a launch.
 * 2. The Configuration page: Meeting Recording › Record Virtual Participant
 *    meeting video (`recording.recordVpMeetingVideo`), read once when the
 *    Virtual Participant starts (stack-settings.ts). When it cannot be read,
 *    the shipped default (on) applies, as for the WebSocket transcriber's
 *    recordScreenVideo.
 * 3. The launch: the Create Virtual Participant form's Record meeting video
 *    switch, passed as ENABLE_VIDEO_RECORDING (absent, on).
 *
 * The outcome is applied to ENABLE_VIDEO_RECORDING before details.ts reads it,
 * so the video recorder, the key-frame sampler and the consent notice's video
 * sentence all follow it. When the launch asked for video and is refused it,
 * the reason is kept in VIDEO_RECORDING_OFF_REASON, which the Virtual
 * Participant writes to its record when it starts (index.ts).
 */

/** The task definition's copy of the EnableVideoRecording stack parameter. */
export const DEPLOYMENT_VIDEO_RECORDING_VARIABLE = 'DEPLOYMENT_VIDEO_RECORDING';
/** The launch's Record meeting video switch, and after this rule, the outcome. */
export const ENABLE_VIDEO_RECORDING_VARIABLE = 'ENABLE_VIDEO_RECORDING';
/** Why a launch that asked for video records none; unset when it records, or did not ask. */
export const VIDEO_RECORDING_OFF_REASON_VARIABLE = 'VIDEO_RECORDING_OFF_REASON';

/** The shipped default of recording.recordVpMeetingVideo (setting_defaults.json). */
export const RECORD_VP_MEETING_VIDEO_DEFAULT = true;

/**
 * The values of the VP record's `videoRecordingOffReason`: the deployment does
 * not record video, or the Configuration page setting is off.
 */
export type VideoRecordingOffReason = 'deployment' | 'configuration';

export interface VideoRecordingOutcome {
    /** Whether the launch asked for video. */
    readonly requested: boolean;
    /** Whether this Virtual Participant records video. */
    readonly recorded: boolean;
    /** Why a requested video is not recorded; null when it is, or was not requested. */
    readonly offReason: VideoRecordingOffReason | null;
}

/**
 * The outcome for a launch, from the environment and the setting's saved value
 * (undefined when the configuration holds none or could not be read).
 */
export const videoRecordingOutcome = (
    env: Record<string, string | undefined>,
    setting: boolean | undefined,
): VideoRecordingOutcome => {
    const requested = env[ENABLE_VIDEO_RECORDING_VARIABLE] !== 'false';
    if (!requested) return { requested, recorded: false, offReason: null };
    if (env[DEPLOYMENT_VIDEO_RECORDING_VARIABLE] === 'false') {
        return { requested, recorded: false, offReason: 'deployment' };
    }
    if ((setting ?? RECORD_VP_MEETING_VIDEO_DEFAULT) === false) {
        return { requested, recorded: false, offReason: 'configuration' };
    }
    return { requested, recorded: true, offReason: null };
};

/**
 * Applies the outcome to `env`: ENABLE_VIDEO_RECORDING is set to false when the
 * launch's video is refused, and VIDEO_RECORDING_OFF_REASON says why (and is
 * removed otherwise). Returns the outcome, for the caller's log line.
 */
export const applyVideoRecordingRule = (
    env: Record<string, string | undefined>,
    setting: boolean | undefined,
): VideoRecordingOutcome => {
    const outcome = videoRecordingOutcome(env, setting);
    if (outcome.offReason) {
        env[ENABLE_VIDEO_RECORDING_VARIABLE] = 'false';
        env[VIDEO_RECORDING_OFF_REASON_VARIABLE] = outcome.offReason;
    } else {
        delete env[VIDEO_RECORDING_OFF_REASON_VARIABLE];
    }
    return outcome;
};

/** The reason this Virtual Participant's requested video is not recorded, if any. */
export const videoRecordingOffReason = (
    env: Record<string, string | undefined> = process.env,
): VideoRecordingOffReason | null => {
    const value = env[VIDEO_RECORDING_OFF_REASON_VARIABLE];
    return value === 'deployment' || value === 'configuration' ? value : null;
};

/** The note stack-settings.ts logs for a refused video. */
export const videoRecordingNote = (outcome: VideoRecordingOutcome): string | null => {
    if (outcome.offReason === 'deployment') {
        return 'meeting video is not recorded: this deployment does not record video (EnableVideoRecording is false)';
    }
    if (outcome.offReason === 'configuration') {
        return 'meeting video is not recorded: Meeting Recording › Record Virtual Participant meeting video is off';
    }
    return null;
};
