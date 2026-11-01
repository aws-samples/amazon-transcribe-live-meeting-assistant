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
});
