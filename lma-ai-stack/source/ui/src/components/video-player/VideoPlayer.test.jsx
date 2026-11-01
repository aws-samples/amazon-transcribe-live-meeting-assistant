/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The recording plays from the time a key frame asks for, once the video can
 * be positioned.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/react';

vi.mock('../../contexts/app', () => ({
  default: () => ({ setErrorMessage: vi.fn(), currentCredentials: {} }),
}));
vi.mock('../common/generate-s3-presigned-url', () => ({
  default: vi.fn().mockResolvedValue('https://signed.example/recording.mp4'),
}));

// eslint-disable-next-line import/first
import { VideoPlayer } from './VideoPlayer';

describe('VideoPlayer', () => {
  it('plays from a requested time once its metadata has loaded', async () => {
    const play = vi.fn().mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.play = play;
    const url = 'https://bucket.s3.us-east-1.amazonaws.com/lma-video-recordings%2Fm.mp4';
    const { container, rerender } = render(<VideoPlayer videoRecordingUrl={url} />);
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    const video = container.querySelector('video');

    rerender(<VideoPlayer videoRecordingUrl={url} seekRequest={{ seconds: 41.5, at: 1 }} />);
    // jsdom reports no metadata yet (readyState 0), so the seek waits for it.
    expect(play).not.toHaveBeenCalled();
    fireEvent(video, new Event('loadedmetadata'));
    expect(video.currentTime).toBe(41.5);
    expect(play).toHaveBeenCalledTimes(1);
  });

  it('does not apply the same request again when the link is signed again', async () => {
    const play = vi.fn().mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.play = play;
    const original = Object.getOwnPropertyDescriptor(window.HTMLMediaElement.prototype, 'readyState');
    Object.defineProperty(window.HTMLMediaElement.prototype, 'readyState', { configurable: true, get: () => 4 });
    try {
      const url = 'https://bucket.s3.us-east-1.amazonaws.com/lma-video-recordings%2Fm.mp4';
      const request = { seconds: 41.5, at: 7 };
      const { container, rerender } = render(<VideoPlayer videoRecordingUrl={url} seekRequest={request} />);
      await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
      await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
      const video = container.querySelector('video');
      video.currentTime = 300; // the viewer moved on
      // A credential refresh signs the URL again; the request is unchanged.
      rerender(<VideoPlayer videoRecordingUrl={`${url}?refresh`} seekRequest={request} />);
      await new Promise((r) => {
        setTimeout(r, 20);
      });
      expect(video.currentTime).toBe(300);
      expect(play).toHaveBeenCalledTimes(1);
      rerender(<VideoPlayer videoRecordingUrl={`${url}?refresh`} seekRequest={{ seconds: 12, at: 8 }} />);
      expect(video.currentTime).toBe(12);
      expect(play).toHaveBeenCalledTimes(2);
    } finally {
      Object.defineProperty(window.HTMLMediaElement.prototype, 'readyState', original);
    }
  });
});
