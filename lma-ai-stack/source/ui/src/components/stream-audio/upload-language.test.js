/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it } from 'vitest';

import { uploadErrorMessage, uploadLanguageLabel } from './upload-language';

describe('uploadLanguageLabel', () => {
  it.each([
    ['de-DE', 'German (Germany), de-DE'],
    ['ja-JP', 'Japanese (Japan), ja-JP'],
    ['es-US', 'Spanish (United States), es-US'],
    ['en-GB', 'English (United Kingdom), en-GB'],
    [' pt-BR ', 'Portuguese (Brazil), pt-BR'],
  ])('names the language of %s as the meeting page does', (code, label) => {
    expect(uploadLanguageLabel(code)).toBe(label);
  });

  it.each([
    ['identify-language', 'Identified by Amazon Transcribe (one language)'],
    ['identify-multiple-languages', 'Identified by Amazon Transcribe (several languages)'],
  ])('uses the meeting page wording for %s', (mode, label) => {
    expect(uploadLanguageLabel(mode)).toBe(label);
  });

  it('keeps a code it cannot name', () => {
    expect(uploadLanguageLabel('qq-QQ')).toBe('qq-QQ');
  });

  it.each([[null], [undefined], [''], ['  ']])('says nothing when the response has no language (%p)', (code) => {
    expect(uploadLanguageLabel(code)).toBeNull();
  });
});

describe('uploadErrorMessage', () => {
  it('shows the reason a GraphQL error gives', () => {
    const error = {
      data: null,
      errors: [
        {
          message:
            'The Amazon Transcribe settings on the Configuration page cannot be used for an upload: ' +
            'identify-language needs language options',
        },
      ],
    };
    expect(uploadErrorMessage(error)).toBe(
      'The Amazon Transcribe settings on the Configuration page cannot be used for an upload: ' +
        'identify-language needs language options',
    );
  });

  it('joins several GraphQL errors and skips blank ones', () => {
    expect(uploadErrorMessage({ errors: [{ message: 'one.' }, { message: ' ' }, { message: 'two.' }] })).toBe(
      'one. two.',
    );
  });

  it('shows an Error by its message', () => {
    expect(uploadErrorMessage(new Error('Network error while uploading to S3.'))).toBe(
      'Network error while uploading to S3.',
    );
  });

  it('falls back to the value itself', () => {
    expect(uploadErrorMessage('boom')).toBe('boom');
  });
});
