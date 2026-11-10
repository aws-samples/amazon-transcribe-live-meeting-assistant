/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A control frame for the WebSocket streaming API (docs/websocket-streaming-api.md),
 * from the Stream Audio form's state.
 *
 * The form keeps the two speaker names under their older names, `agentId` and
 * `fromNumber`. The streaming API and the createUploadMeeting input document
 * them as `localSpeakerName` (the microphone) and `remoteSpeakerName` (the
 * meeting audio), so the frame carries those instead.
 * `toNumber` is left out: the transcriber does not use it. A START frame names
 * the client, so the meeting record says it was captured by Stream Audio.
 */
export const STREAM_AUDIO_CLIENT_TYPE = 'STREAM_AUDIO';

export const toStreamingFrame = ({ agentId, fromNumber, toNumber, ...frame }) => ({
  ...frame,
  ...(frame.callEvent === 'START' ? { clientType: STREAM_AUDIO_CLIENT_TYPE } : {}),
  ...(agentId === undefined ? {} : { localSpeakerName: agentId }),
  ...(fromNumber === undefined ? {} : { remoteSpeakerName: fromNumber }),
});

export default toStreamingFrame;
