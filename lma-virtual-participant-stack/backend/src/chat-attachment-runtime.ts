/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The chat attachment capture (chat-attachments.ts) wired to this Virtual
 * Participant: the settings, the `details.start` switch, the recordings bucket
 * and the Kinesis writer. meeting-chat-runtime.ts hands it the attachments of
 * each chat message it adds.
 *
 * The object is written with the bucket's default encryption, as the audio
 * recordings and the key frames are, and as a download: its stored
 * Content-Disposition says so to anything that reads it directly.
 */
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { details } from './details.js';
import {
    ChatAttachmentCapture,
    captureChatAttachmentsSetting,
    chatAttachmentSummariesSetting,
} from './chat-attachments.js';
import { kinesisStreamManager } from './kinesis-stream.js';
import { createLogger } from './logger.js';

const log = createLogger('chat-attachments');

const s3 = new S3Client({ region: process.env.AWS_REGION || 'us-east-1' });

export const chatAttachments = new ChatAttachmentCapture({
    settingOn: () => captureChatAttachmentsSetting(),
    summariesOn: () => chatAttachmentSummariesSetting(),
    capturing: () => details.start,
    callId: () => kinesisStreamManager.currentCallId,
    upload: async (key, body, contentType) => {
        if (!details.recordingsBucketName) {
            log.warn('[chat-attachments] RECORDINGS_BUCKET_NAME is not set; the attachment is not stored');
            throw new Error('no recordings bucket');
        }
        await s3.send(
            new PutObjectCommand({
                Bucket: details.recordingsBucketName,
                Key: key,
                Body: body,
                ContentType: contentType,
                ContentDisposition: 'attachment',
            }),
        );
    },
    emit: (event) => kinesisStreamManager.sendChatAttachment(event),
    // The chat line as the meeting stores it: a rejoined session's prefix and offset.
    onMeetingTimeline: (segmentId, timelineSeconds) => ({
        segmentId: kinesisStreamManager.meetingSegmentId(segmentId),
        timelineSeconds: kinesisStreamManager.meetingSeconds(timelineSeconds),
    }),
});
