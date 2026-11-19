/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ChatAttachments, { ChatAttachment, ChatAttachmentsContext, notStoredText } from './ChatAttachments';
import ChatSegment from './ChatSegment';
import { attachmentsPending, groupBySegment } from '../../hooks/use-chat-attachments';

const image = {
  AttachmentId: '0000012500-aa',
  SegmentId: 'chat-1',
  Kind: 'IMAGE',
  FileName: 'whiteboard.png',
  Status: 'STORED',
  ContentType: 'image/png',
  Size: 2048,
  Summarize: true,
  Title: 'Rollout plan sketch',
  Summary: 'A diagram of the three rollout phases.',
  SummaryState: 'DONE',
  PreviewUrl: 'https://rec-bucket.s3.amazonaws.com/x?response-content-disposition=inline',
  DownloadUrl: 'https://rec-bucket.s3.amazonaws.com/x?response-content-disposition=attachment',
};
const pdf = {
  ...image,
  AttachmentId: '0000013000-bb',
  Kind: 'DOCUMENT',
  FileName: 'Q3 plan.pdf',
  ContentType: 'application/pdf',
  PreviewUrl: null,
  Title: 'Q3 plan',
  Summary: '<b>bold</b> **not markdown**',
};
const card = {
  AttachmentId: '0000014000-cc',
  SegmentId: 'chat-1',
  Kind: 'DOCUMENT',
  FileName: 'Budget.docx',
  Status: 'NOT_STORED',
  NotStoredReason: 'NOT_DOWNLOADABLE',
};

describe('ChatAttachment', () => {
  it('shows an image as a thumbnail from its preview link, with its AI title and summary', () => {
    const { container } = render(<ChatAttachment attachment={image} onOpen={vi.fn()} />);
    const img = container.querySelector('img.chat-attachment-thumb');
    expect(img.getAttribute('src')).toBe(image.PreviewUrl);
    expect(img.getAttribute('alt')).toBe('Rollout plan sketch');
    expect(screen.getByText('Rollout plan sketch')).toBeTruthy();
    expect(screen.getByText('A diagram of the three rollout phases.')).toBeTruthy();
    expect(screen.getByText('AI-generated from the attachment')).toBeTruthy();
  });

  it('shows a document as a file chip, never as an image or embedded content', () => {
    const { container } = render(<ChatAttachment attachment={pdf} onOpen={vi.fn()} />);
    expect(container.querySelector('img, iframe, object, embed')).toBeNull();
    expect(screen.getByText('Q3 plan.pdf')).toBeTruthy();
  });

  it('never shows a thumbnail without a preview link, whatever the stored type says', () => {
    const svg = { ...image, ContentType: 'image/svg+xml', PreviewUrl: null };
    const { container } = render(<ChatAttachment attachment={svg} onOpen={vi.fn()} />);
    expect(container.querySelector('img')).toBeNull();
  });

  it('shows the title and summary as text, with no HTML or markdown made of them', () => {
    const { container } = render(<ChatAttachment attachment={pdf} onOpen={vi.fn()} />);
    expect(container.querySelector('b')).toBeNull();
    expect(screen.getByText('<b>bold</b> **not markdown**')).toBeTruthy();
  });

  it('opens an attachment through its id, for a fresh link', () => {
    const onOpen = vi.fn();
    render(<ChatAttachment attachment={pdf} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole('button', { name: 'Download Q3 plan.pdf' }));
    expect(onOpen).toHaveBeenCalledWith('0000013000-bb');
  });

  it('names an attachment that was not stored, says why, and offers nothing to open', () => {
    const { container } = render(<ChatAttachment attachment={card} onOpen={vi.fn()} />);
    expect(screen.getByText('Budget.docx')).toBeTruthy();
    expect(container.textContent).toContain(notStoredText('NOT_DOWNLOADABLE'));
    expect(container.querySelector('button, img, a')).toBeNull();
  });
});

describe('ChatAttachments on a chat line', () => {
  const segment = {
    segmentId: 'chat-1',
    channel: 'MEETING_CHAT',
    speaker: 'Alice',
    transcript: '(attachment: whiteboard.png)',
    startTime: 1,
  };

  it('shows the attachments of its own line from the page context', () => {
    const value = { bySegment: groupBySegment([pdf, image, { ...card, SegmentId: 'chat-2' }]), open: vi.fn() };
    render(
      <ChatAttachmentsContext.Provider value={value}>
        <ChatSegment segment={segment} time="0:01" />
      </ChatAttachmentsContext.Provider>,
    );
    const shown = screen.getAllByTestId('chat-attachment').map((el) => el.textContent);
    expect(shown).toHaveLength(2);
    expect(shown[0]).toContain('whiteboard.png');
    expect(shown[1]).toContain('Q3 plan.pdf');
  });

  it('shows nothing on a page that provides no attachments', () => {
    const { container } = render(<ChatAttachments segmentId="chat-1" />);
    expect(container.innerHTML).toBe('');
  });
});

describe('reading the attachments again', () => {
  it('is due while a line names an attachment that has not arrived, or a title is being written', () => {
    expect(attachmentsPending({}, ['chat-1'])).toBe(true);
    expect(attachmentsPending(groupBySegment([image]), ['chat-1'])).toBe(false);
    expect(attachmentsPending(groupBySegment([{ ...image, SummaryState: null }]), ['chat-1'])).toBe(true);
    expect(attachmentsPending(groupBySegment([{ ...image, SummaryState: null, Summarize: false }]), [])).toBe(false);
    expect(attachmentsPending(groupBySegment([card]), ['chat-1'])).toBe(false);
  });
});
