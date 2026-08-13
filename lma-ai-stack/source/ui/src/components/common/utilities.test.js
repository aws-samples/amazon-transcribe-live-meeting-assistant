/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { splitDiarizationLabel } from './utilities';

/**
 * `splitDiarizationLabel` exists because two places (CallPanel.jsx and
 * download-func.js) compare a segment's speaker against DEFAULT_OTHER_SPEAKER_NAME
 * to substitute the user's chosen participant label. Once the websocket
 * transcriber appends a per-channel diarization label the speaker reads
 * 'Other Participant (spk_0)', so an exact match silently stops firing — these
 * tests pin the base/suffix split that keeps both comparisons working.
 */
describe('splitDiarizationLabel', () => {
  it('splits a diarization label off the base speaker name', () => {
    expect(splitDiarizationLabel('Other Participant (spk_0)')).toEqual({
      base: 'Other Participant',
      suffix: ' (spk_0)',
    });
  });

  it('leaves an unlabelled speaker untouched', () => {
    expect(splitDiarizationLabel('Other Participant')).toEqual({
      base: 'Other Participant',
      suffix: '',
    });
  });

  it('handles multi-digit speaker numbers', () => {
    // Transcribe can return up to spk_29.
    expect(splitDiarizationLabel('Alice (spk_12)')).toEqual({ base: 'Alice', suffix: ' (spk_12)' });
  });

  it('re-joining base and suffix reproduces the original string', () => {
    // The callers substitute the base and re-attach the suffix, so this identity
    // is what guarantees a label is never dropped.
    ['Other Participant (spk_1)', 'Me', 'a@b.com (spk_0)', ''].forEach((speaker) => {
      const { base, suffix } = splitDiarizationLabel(speaker);
      expect(`${base}${suffix}`).toBe(speaker);
    });
  });

  it('does not treat a lookalike name as a label', () => {
    // Only the exact trailing '(spk_<digits>)' form is a diarization label; a
    // participant literally called '(spk_x)' or a mid-string match must not be
    // stripped, or the substitution would rewrite a real name.
    expect(splitDiarizationLabel('Speaker (spk_x)')).toEqual({
      base: 'Speaker (spk_x)',
      suffix: '',
    });
    expect(splitDiarizationLabel('(spk_0) leading').base).toBe('(spk_0) leading');
  });

  it('tolerates a missing speaker', () => {
    expect(splitDiarizationLabel(undefined)).toEqual({ base: '', suffix: '' });
    expect(splitDiarizationLabel(null)).toEqual({ base: '', suffix: '' });
  });
});
