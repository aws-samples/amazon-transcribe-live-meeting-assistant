/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting chat capture (meeting-chat.ts) wired to this Virtual
 * Participant: the setting, the `details.start` switch, the transcript
 * timeline and the Kinesis writer.
 *
 * Kept apart from the capture itself so its tests do not load the
 * transcription service.
 */
import { details } from './details.js';
import { MeetingChatCapture, captureMeetingChatSetting, contentRedactionRequested } from './meeting-chat.js';
import { transcriptionService } from './scribe.js';
import { kinesisStreamManager } from './kinesis-stream.js';

export const meetingChat = new MeetingChatCapture({
    settingOn: () => captureMeetingChatSetting(),
    // The saved flag, whatever the language; see contentRedactionRequested.
    redacting: () => contentRedactionRequested(),
    capturing: () => details.start,
    meetingStarted: () => kinesisStreamManager.meetingStarted,
    // The clock the speech segments and the key frames are on.
    timelineSeconds: () => transcriptionService.timelineSeconds(),
    send: (segment) => kinesisStreamManager.sendChatSegment(segment),
    ownIdentities: () => [details.lmaIdentity, details.scribeIdentity, details.scribeName],
});
