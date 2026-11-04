/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A control frame for the WebSocket streaming API (docs/websocket-streaming-api.md),
 * from the Stream Audio form's state.
 *
 * The form keeps the two speaker names as `agentId` and `fromNumber` because the
 * same state also fills the createUploadMeeting input, which takes those names.
 * The streaming API documents them as `localSpeakerName` (the microphone) and
 * `remoteSpeakerName` (the meeting audio), so the frame carries those instead.
 * `toNumber` is left out: the transcriber does not use it.
 */
export const toStreamingFrame = ({ agentId, fromNumber, toNumber, ...frame }) => ({
  ...frame,
  ...(agentId === undefined ? {} : { localSpeakerName: agentId }),
  ...(fromNumber === undefined ? {} : { remoteSpeakerName: fromNumber }),
});

export default toStreamingFrame;
