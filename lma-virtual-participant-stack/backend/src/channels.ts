/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The transcript channel names a Virtual Participant writes to Kinesis.
 *
 * REMOTE carries the meeting's participants (ch_0, meeting_audio.monitor); LOCAL
 * carries the VP's own voice assistant (ch_1, agent_output.monitor). These are
 * the names Stream Audio uses for its meeting and microphone channels too. LMA
 * before 0.4.0 wrote them as CALLER and AGENT; the call event processor still
 * accepts those from any producer (lma_channels in the AI stack's configuration
 * layer). The VP never reads a channel name back, so it needs no aliases.
 */
export const REMOTE_CHANNEL = 'REMOTE';
export const LOCAL_CHANNEL = 'LOCAL';

export type SpeechChannel = typeof REMOTE_CHANNEL | typeof LOCAL_CHANNEL;

/**
 * The meeting chat: one segment per message a participant sent to everyone,
 * written by meeting-chat.ts. Not a speech channel, so it is not part of
 * SpeechChannel; the AI stack's lma_channels.MEETING_CHAT names the same value.
 */
export const MEETING_CHAT_CHANNEL = 'MEETING_CHAT';
