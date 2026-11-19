/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting chat capture (meeting-chat.ts) wired to this Virtual
 * Participant: the setting, the `details.start` switch, the transcript
 * timeline, the Kinesis writer and the attachment capture
 * (chat-attachment-runtime.ts).
 *
 * Kept apart from the capture itself so its tests do not load the
 * transcription service.
 */
import { details } from './details.js';
import { MeetingChatCapture, captureMeetingChatSetting, contentRedactionRequested } from './meeting-chat.js';
import { transcriptionService } from './scribe.js';
import { kinesisStreamManager } from './kinesis-stream.js';
import { chatAttachments } from './chat-attachment-runtime.js';

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
    ownName: () => details.scribeIdentity || details.scribeName,
    // The images and files of each message added; stored when the setting is on.
    attachments: (segmentId, platform, at, list) => {
        chatAttachments.capture(segmentId, platform, at, list);
    },
});
