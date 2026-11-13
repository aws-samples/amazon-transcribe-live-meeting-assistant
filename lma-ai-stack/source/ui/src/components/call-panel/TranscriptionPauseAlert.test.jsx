/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The alert that transcription is paused from the meeting chat: shown only for
 * a live meeting whose Call.TranscriptionPause reads paused, with since when
 * and by whom, and gone once it reads resumed or the meeting ends.
 */
import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import TranscriptionPauseAlert, { parseTranscriptionPause } from './TranscriptionPauseAlert';

const AT = '2026-11-12T10:15:00.000Z';
const PAUSED = JSON.stringify({ Paused: true, At: AT, By: 'Alice Example' });
const RESUMED = JSON.stringify({ Paused: false, At: '2026-11-12T10:20:00.000Z' });
const HEADER = 'Transcription is paused';

describe('TranscriptionPauseAlert', () => {
  it('says since when and by whom, and how to resume', () => {
    render(<TranscriptionPauseAlert isLive transcriptionPause={PAUSED} />);
    expect(screen.getByText(HEADER)).toBeTruthy();
    const body = screen.getByText(/paused from the meeting chat/);
    expect(body.textContent).toContain(`since ${new Date(AT).toLocaleString()}`);
    expect(body.textContent).toContain('by Alice Example');
    expect(body.textContent).toContain('"LMA start" in the meeting chat');
  });

  it('leaves out the sender when the chat showed none', () => {
    render(<TranscriptionPauseAlert isLive transcriptionPause={JSON.stringify({ Paused: true, At: AT })} />);
    expect(screen.getByText(/paused from the meeting chat/).textContent).not.toContain(' by ');
  });

  it('goes when transcription resumes', () => {
    const { rerender } = render(<TranscriptionPauseAlert isLive transcriptionPause={PAUSED} />);
    expect(screen.getByText(HEADER)).toBeTruthy();
    rerender(<TranscriptionPauseAlert isLive transcriptionPause={RESUMED} />);
    expect(screen.queryByText(HEADER)).toBeNull();
  });

  it('goes when the meeting ends', () => {
    const { rerender } = render(<TranscriptionPauseAlert isLive transcriptionPause={PAUSED} />);
    rerender(<TranscriptionPauseAlert isLive={false} transcriptionPause={PAUSED} />);
    expect(screen.queryByText(HEADER)).toBeNull();
  });

  it('shows nothing for a meeting never paused, or with an unreadable value', () => {
    const { rerender } = render(<TranscriptionPauseAlert isLive />);
    expect(screen.queryByText(HEADER)).toBeNull();
    rerender(<TranscriptionPauseAlert isLive transcriptionPause="not json" />);
    expect(screen.queryByText(HEADER)).toBeNull();
  });

  it('stays dismissed until the next pause', () => {
    const { rerender } = render(<TranscriptionPauseAlert isLive transcriptionPause={PAUSED} />);
    fireEvent.click(screen.getByLabelText('Dismiss'));
    expect(screen.queryByText(HEADER)).toBeNull();
    rerender(<TranscriptionPauseAlert isLive transcriptionPause={RESUMED} />);
    rerender(
      <TranscriptionPauseAlert
        isLive
        transcriptionPause={JSON.stringify({ Paused: true, At: '2026-11-12T10:30:00.000Z' })}
      />,
    );
    expect(screen.getByText(HEADER)).toBeTruthy();
  });
});

describe('parseTranscriptionPause', () => {
  it('reads the stored JSON', () => {
    expect(parseTranscriptionPause(PAUSED)).toEqual({ paused: true, at: AT, by: 'Alice Example' });
    expect(parseTranscriptionPause(RESUMED)).toEqual({ paused: false, at: '2026-11-12T10:20:00.000Z', by: null });
  });

  it('reads a value without a Paused flag as nothing', () => {
    expect(parseTranscriptionPause(JSON.stringify({ At: AT }))).toBeNull();
    expect(parseTranscriptionPause(null)).toBeNull();
  });
});
