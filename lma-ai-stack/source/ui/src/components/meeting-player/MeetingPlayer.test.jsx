/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting player: which element it plays, its chapter marks, its
 * controls, a seek asked for before the recording has loaded, and what it
 * tells the page about where it is.
 */
import React, { createRef } from 'react';
import { describe, it, expect, vi, beforeAll } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';

import { installMediaElementStub, loadMedia } from './media-element-stub';
import { createPlaybackStore } from './playback-store';

vi.mock('../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));

// eslint-disable-next-line import/first
import MeetingPlayer from './MeetingPlayer';

const CHAPTERS = [
  { seconds: 0, label: '00:00', title: 'Welcome' },
  { seconds: 300, label: '05:00', title: 'Budget review' },
];

beforeAll(installMediaElementStub);

const renderPlayer = async (props = {}) => {
  const controllerRef = createRef();
  const store = createPlaybackStore();
  const sign = vi.fn().mockResolvedValue('https://signed.example/recording');
  const view = render(
    <MeetingPlayer
      kind="video"
      url="https://bucket.s3.us-east-1.amazonaws.com/v.mp4"
      chapters={CHAPTERS}
      store={store}
      controllerRef={controllerRef}
      sign={sign}
      // eslint-disable-next-line react/jsx-props-no-spreading
      {...props}
    />,
  );
  const media = view.container.querySelector('video, audio');
  await waitFor(() => expect(media.getAttribute('src')).toBe('https://signed.example/recording'));
  return { ...view, media, store, controllerRef, sign };
};

describe('MeetingPlayer', () => {
  it('plays the video in a video element, and the audio in an audio element', async () => {
    const { media, unmount } = await renderPlayer();
    expect(media.tagName).toBe('VIDEO');
    unmount();
    const audio = await renderPlayer({ kind: 'audio' });
    expect(audio.media.tagName).toBe('AUDIO');
    expect(screen.queryByRole('button', { name: 'Full screen' })).toBeNull();
  });

  it('names every control', async () => {
    const { media } = await renderPlayer();
    loadMedia(media, 600);
    expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy();
    expect(screen.getByRole('slider', { name: 'Seek' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Playback speed, 1x/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Mute' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeTruthy();
  });

  it('marks the chapters on the seek bar at their share of the recording, titled', async () => {
    const { media } = await renderPlayer();
    loadMedia(media, 600);
    const markers = screen.getAllByTestId('meeting-player-marker');
    expect(markers.map((m) => m.style.left)).toEqual(['0%', '50%']);
    expect(markers[1].getAttribute('aria-label')).toBe('Chapter at 05:00: Budget review');
    expect(markers[1].textContent).toContain('Budget review');
  });

  it('places the marks on the recording clock', async () => {
    const { media } = await renderPlayer({ clock: { anchors: [{ transcript: 0, recording: 60 }] } });
    loadMedia(media, 600);
    expect(screen.getAllByTestId('meeting-player-marker').map((m) => m.style.left)).toEqual(['10%', '60%']);
  });

  it('plays from a chapter mark when it is clicked', async () => {
    const { media } = await renderPlayer();
    loadMedia(media, 600);
    fireEvent.click(screen.getByRole('button', { name: 'Chapter at 05:00: Budget review' }));
    expect(media.currentTime).toBe(300);
    expect(media.paused).toBe(false);
    expect(screen.getByTestId('meeting-player-chapter').textContent).toBe('Chapter: Budget review');
  });

  it('keeps a seek asked for before the recording has loaded, and applies it once it has', async () => {
    const { media, controllerRef } = await renderPlayer();
    act(() => controllerRef.current.seek(120, { play: true }));
    expect(media.currentTime).toBe(0);
    loadMedia(media, 600);
    expect(media.currentTime).toBe(120);
    expect(media.paused).toBe(false);
  });

  it('moves without playing when asked to, and pauses a recording that was playing', async () => {
    const { media, controllerRef } = await renderPlayer();
    loadMedia(media, 600);
    act(() => controllerRef.current.seek(10, { play: true }));
    act(() => controllerRef.current.seek(42, { play: false }));
    expect(media.currentTime).toBe(42);
    expect(media.paused).toBe(true);
  });

  it('tells the page where it is on the transcript clock', async () => {
    const { media, controllerRef, store } = await renderPlayer({
      clock: { anchors: [{ transcript: 100, recording: 130 }] },
    });
    expect(store.get().engaged).toBe(false);
    loadMedia(media, 600);
    act(() => controllerRef.current.seek(160, { play: true }));
    expect(store.get()).toMatchObject({ seconds: 130, playing: true, engaged: true, seekCount: 1 });
    act(() => controllerRef.current.toggle());
    expect(store.get().playing).toBe(false);
  });

  it('moves by a number of seconds, within the recording', async () => {
    const { media, controllerRef } = await renderPlayer();
    loadMedia(media, 600);
    act(() => controllerRef.current.seek(3, {}));
    act(() => controllerRef.current.nudge(-5));
    expect(media.currentTime).toBe(0);
    act(() => controllerRef.current.nudge(5));
    expect(media.currentTime).toBe(5);
  });

  it('plays and pauses from its button', async () => {
    const { media } = await renderPlayer();
    loadMedia(media, 600);
    fireEvent.click(screen.getByRole('button', { name: 'Play' }));
    expect(media.paused).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(media.paused).toBe(true);
  });

  it('offers the other recording when the meeting has both', async () => {
    const onSwitch = vi.fn();
    await renderPlayer({ alternative: 'audio', onSwitch });
    fireEvent.click(screen.getByRole('button', { name: 'Audio only' }));
    expect(onSwitch).toHaveBeenCalledWith('audio');
  });

  it('signs the link again when the recording stops loading, and carries on from the same place', async () => {
    const { media, sign } = await renderPlayer();
    loadMedia(media, 600);
    // eslint-disable-next-line no-param-reassign
    media.currentTime = 200;
    sign.mockResolvedValueOnce('https://signed.example/renewed');
    fireEvent(media, new Event('error'));
    await waitFor(() => expect(media.getAttribute('src')).toBe('https://signed.example/renewed'));
    expect(sign).toHaveBeenCalledTimes(2);
    loadMedia(media, 600);
    expect(media.currentTime).toBe(200);
  });

  it('says so when the recording cannot be loaded', async () => {
    render(
      <MeetingPlayer kind="audio" url="https://b.s3.us-east-1.amazonaws.com/a.wav" sign={() => Promise.reject()} />,
    );
    expect(await screen.findByText('The recording could not be loaded.')).toBeTruthy();
  });
});
