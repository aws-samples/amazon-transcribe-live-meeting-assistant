/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable import/prefer-default-export */

/**
 * Split a transcript segment's Speaker string into its base name and its
 * Amazon Transcribe diarization label, e.g.
 *   'Other Participant (spk_0)' -> { base: 'Other Participant', suffix: ' (spk_0)' }
 *   'Other Participant'         -> { base: 'Other Participant', suffix: '' }
 *
 * The websocket transcriber appends '(spk_N)' when per-channel speaker
 * identification is enabled. Callers that compare a speaker name against a known
 * default must compare the BASE — an exact match against the whole string
 * silently stops working the moment a label is appended — and then re-attach the
 * suffix so the label survives the substitution.
 */
export const splitDiarizationLabel = (speaker) => {
  const value = speaker ?? '';
  const match = /^(.*?)(\s*\(spk_\d+\))$/.exec(value);
  return match ? { base: match[1], suffix: match[2] } : { base: value, suffix: '' };
};

export const getTimestampStr = () => {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  const hour = String(now.getHours()).padStart(2, '0');
  const minute = String(now.getMinutes()).padStart(2, '0');
  const second = String(now.getSeconds()).padStart(2, '0');
  const millisecond = String(now.getMilliseconds()).padStart(3, '0');
  const formattedDate = `${year}-${month}-${day}-${hour}:${minute}:${second}.${millisecond}`;
  return formattedDate;
};
