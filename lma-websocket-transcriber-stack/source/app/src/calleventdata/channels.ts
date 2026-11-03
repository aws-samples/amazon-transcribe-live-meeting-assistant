/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The transcript channel names this server writes to Kinesis.
 *
 * REMOTE is the meeting audio (the browser tab or meeting app, channel 0) and
 * LOCAL the user's microphone (channel 1). LMA before 0.4.0 wrote them as CALLER
 * and AGENT; the call event processor still accepts those from any producer and
 * stores them as REMOTE and LOCAL (lma_channels in the AI stack's configuration
 * layer). This server never reads a channel name back, so it needs no aliases.
 */
import { CHANNEL_SYSTEM } from './eventtypes';

export const REMOTE_CHANNEL = 'REMOTE';
export const LOCAL_CHANNEL = 'LOCAL';

/**
 * The transcript channel for an audio channel id. Only the meeting channel,
 * ch_0, is REMOTE; any other id, or none, is LOCAL. Shared by both engines so
 * Amazon Transcribe and the MicroVM ASR write the same channels.
 */
export const transcriptChannelFor = (channelId: string | undefined): string =>
    channelId === CHANNEL_SYSTEM ? REMOTE_CHANNEL : LOCAL_CHANNEL;
