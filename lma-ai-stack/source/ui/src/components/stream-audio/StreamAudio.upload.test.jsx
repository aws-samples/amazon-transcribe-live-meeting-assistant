/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Upload Audio page's language, with AppSync and the S3 PUT faked.
 *
 * - The page sends no languageCode, so the upload follows the Configuration
 *   page's Amazon Transcribe language.
 * - The completion message names the language createUploadMeeting applied, and
 *   says nothing of it when an older backend leaves it out.
 * - An upload the settings refuse shows the reason the server gave.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const appsync = vi.hoisted(() => ({ graphql: null }));

vi.mock('react-use-websocket', () => ({ default: () => ({ sendMessage: () => undefined }) }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: (...args) => appsync.graphql(...args) }) }));
vi.mock('aws-amplify/auth', () => ({
  fetchUserAttributes: vi.fn().mockResolvedValue({}),
  fetchAuthSession: vi.fn().mockResolvedValue({}),
}));
vi.mock('../../contexts/app', () => ({
  default: () => ({
    currentSession: { tokens: { accessToken: 'access', idToken: 'id' } },
    user: { username: 'ana' },
    authState: 'authenticated',
  }),
}));
vi.mock('../../contexts/settings', () => ({
  default: () => ({ settings: { WSEndpoint: 'wss://transcriber.example/api/v1/ws' } }),
}));

// eslint-disable-next-line import/first
import StreamAudio from './StreamAudio';

// An S3 PUT that succeeds as soon as it is sent.
function FakeXhr() {
  const xhr = {
    status: 200,
    statusText: 'OK',
    upload: {},
    open: vi.fn(),
    setRequestHeader: vi.fn(),
    abort: vi.fn(),
    send: () => setTimeout(() => xhr.onload(), 0),
  };
  return xhr;
}

const created = (overrides = {}) => ({
  data: {
    createUploadMeeting: {
      callId: 'Uploaded-20260101T000000-abcdef12',
      uploadUrl: 'https://bucket.s3.example/key',
      uploadBucket: 'bucket',
      uploadKey: 'lma-uploads-pending/key/recording.wav',
      contentType: 'audio/wav',
      expiresInSeconds: 900,
      ...overrides,
    },
  },
});

const upload = async () => {
  const { container } = render(<StreamAudio mode="upload" />);
  const input = container.querySelector('input[type="file"]');
  const file = new File(['RIFF'], 'recording.wav', { type: 'audio/wav' });
  fireEvent.change(input, { target: { files: [file] } });
  const submit = await screen.findByRole('button', { name: /upload/i });
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);
};

describe('StreamAudio upload language', () => {
  beforeEach(() => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('sends no language, so the upload follows the Configuration page', async () => {
    appsync.graphql = vi.fn().mockResolvedValue(created({ transcriptionLanguage: 'en-US' }));
    await upload();
    await screen.findByText('Upload complete', { selector: '*' });
    const { input } = appsync.graphql.mock.calls[0][0].variables;
    expect(input).not.toHaveProperty('languageCode');
  });

  it.each([
    ['ja-JP', 'Transcription language: Japanese (Japan), ja-JP'],
    ['fr-CA', 'Transcription language: French (Canada), fr-CA'],
    ['identify-language', 'Transcription language: Identified by Amazon Transcribe (one language)'],
    ['identify-multiple-languages', 'Transcription language: Identified by Amazon Transcribe (several languages)'],
  ])('names the applied language %s when the upload completes', async (code, text) => {
    appsync.graphql = vi.fn().mockResolvedValue(created({ transcriptionLanguage: code }));
    await upload();
    expect((await screen.findByTestId('upload-language')).textContent).toBe(text);
  });

  it('says nothing of the language when the response leaves it out', async () => {
    appsync.graphql = vi.fn().mockResolvedValue(created());
    await upload();
    await screen.findByText(/Meeting ID:/);
    expect(screen.queryByTestId('upload-language')).toBeNull();
  });

  it('shows why the settings cannot be used for an upload', async () => {
    const reason =
      'The Amazon Transcribe settings on the Configuration page cannot be used for an upload: ' +
      'identify-language needs at least 2 language options for an upload';
    appsync.graphql = vi.fn().mockRejectedValue({ data: null, errors: [{ message: reason }] });
    await upload();
    expect((await screen.findAllByText(reason)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Meeting ID:/)).toBeNull();
  });
});
