/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ChatSegment, { ChatSegmentContent, chatSender, UNKNOWN_CHAT_SENDER } from './ChatSegment';

const message = (transcript, speaker = 'Alice') => ({
  segmentId: 'chat-1',
  channel: 'MEETING_CHAT',
  speaker,
  transcript,
  startTime: 62,
  endTime: 62,
});

// The scheme a chat message might hold, spelled out of a literal the linter reads as code.
const SCRIPT = ['java', 'script:'].join('');

const marks = (container) => [...container.querySelectorAll('mark')].map((m) => m.textContent);

describe('ChatSegmentContent', () => {
  it('shows the message as typed, with no markdown or HTML made of it', () => {
    const typed = `**not bold** _nor this_ <b>tag</b> <img src=x onerror="alert(1)"> [doc](${SCRIPT}alert(1))`;
    const { container } = render(<ChatSegmentContent text={typed} />);
    expect(container.textContent).toBe(typed);
    expect(container.querySelector('strong, em, b, img, a, script')).toBeNull();
  });

  it('keeps the line breaks', () => {
    const { container } = render(<ChatSegmentContent text={'one\ntwo\n\nthree'} />);
    const text = container.querySelector('.chat-segment-text');
    expect(text.textContent).toBe('one\ntwo\n\nthree');
    // The stylesheet is not loaded under jsdom; the class carries `white-space: pre-wrap`.
    expect(text.className).toBe('chat-segment-text');
  });

  it('shows each address as code with a copy button, never as a link', () => {
    const { container } = render(
      <ChatSegmentContent text="Deck (https://example.com/deck). Also www.example.org/notes, thanks" />,
    );
    expect([...container.querySelectorAll('code')].map((c) => c.textContent)).toEqual([
      'https://example.com/deck',
      'www.example.org/notes',
    ]);
    expect(container.querySelector('a')).toBeNull();
    expect(screen.getByRole('button', { name: 'Copy https://example.com/deck' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy https://www.example.org/notes' })).toBeTruthy();
    expect(container.textContent).toBe('Deck (https://example.com/deck). Also www.example.org/notes, thanks');
  });

  it('copies the address as shown', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<ChatSegmentContent text="see https://example.com/a?b=1." />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy https://example.com/a?b=1' }));
    expect(writeText).toHaveBeenCalledWith('https://example.com/a?b=1');
  });

  it('shows another scheme as plain text, with no copy button', () => {
    const { container } = render(<ChatSegmentContent text={`${SCRIPT}alert(1) data:text/html,hi`} />);
    expect(container.querySelector('code, a, button')).toBeNull();
    expect(container.textContent).toBe(`${SCRIPT}alert(1) data:text/html,hi`);
  });

  it("marks the search's words in the text and in an address", () => {
    const { container } = render(
      <ChatSegmentContent text="The Budget: https://example.com/budget-2027" query="budget" />,
    );
    expect(marks(container)).toEqual(['Budget', 'budget']);
    expect(container.querySelector('code mark').textContent).toBe('budget');
    expect(container.textContent).toBe('The Budget: https://example.com/budget-2027');
  });

  it('marks nothing with no search', () => {
    const { container } = render(<ChatSegmentContent text="budget" />);
    expect(marks(container)).toEqual([]);
  });

  it('shows nothing for no text', () => {
    const { container } = render(<ChatSegmentContent />);
    expect(container.textContent).toBe('');
  });
});

describe('ChatSegment', () => {
  it('shows the chat icon, "<sender> · chat", the time and the message', () => {
    const { container } = render(<ChatSegment segment={message('+1')} time="01:02.0" />);
    expect(container.querySelector('.chat-segment-label').textContent).toBe('Alice · chat');
    expect(container.querySelector('.chat-segment-icon svg, .chat-segment-icon [class*="icon"]')).toBeTruthy();
    expect(screen.getByText('01:02.0')).toBeTruthy();
    expect(screen.getByText('+1')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Play from/ })).toBeNull();
  });

  it('names an unknown sender', () => {
    expect(chatSender(message('x', '  '))).toBe(UNKNOWN_CHAT_SENDER);
    expect(chatSender({})).toBe(UNKNOWN_CHAT_SENDER);
    expect(chatSender(message('x', ' Bo Chen '))).toBe('Bo Chen');
  });

  it('plays the recording from its time when it can', () => {
    const onSeek = vi.fn();
    render(<ChatSegment segment={message('+1')} time="01:02.0" onSeek={onSeek} />);
    fireEvent.click(screen.getByRole('button', { name: 'Play from 01:02.0' }));
    expect(onSeek).toHaveBeenCalledWith(62);
  });

  it('shows a translation under the message as plain text too', () => {
    const { container } = render(
      <ChatSegment segment={message('hola')} time="01:02.0" translation="**hello** https://example.com" />,
    );
    const translated = container.querySelector('.translated-text');
    expect(translated.textContent).toBe('**hello** https://example.com');
    expect(translated.querySelector('code').textContent).toBe('https://example.com');
    expect(container.querySelector('strong + strong, a')).toBeNull();
  });
});
