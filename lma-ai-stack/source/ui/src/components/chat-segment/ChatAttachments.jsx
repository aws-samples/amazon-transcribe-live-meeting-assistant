/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The attachments of a chat line: a thumbnail for an image, a file chip for a
 * document, with the AI title and summary under each when it has them. See
 * docs/virtual-participant.md, "Chat attachments".
 *
 * - A thumbnail is an <img> only when the resolver gave a PreviewUrl, which it
 *   does for PNG, JPEG, GIF and WebP images alone: no other type is ever shown
 *   in the page.
 * - Opening an attachment reads it again for a fresh link and opens that link,
 *   which S3 serves as a download.
 * - The file name, title and summary are shown as React text, never as markdown
 *   or HTML.
 *
 * The meeting page provides the attachments through ChatAttachmentsContext; a
 * page that does not (the embedded transcript) shows none.
 */
import React, { createContext, useContext } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Icon } from '@cloudscape-design/components';

export const ChatAttachmentsContext = createContext(null);

const NOT_STORED = {
  NOT_DOWNLOADABLE: 'the meeting platform does not let the Virtual Participant download it',
  TOO_LARGE: 'it is larger than the size limit',
  TYPE_NOT_ALLOWED: 'its type is not one that is stored',
  UNAVAILABLE: 'it could not be read from the meeting',
};

/** Why an attachment was recorded by name only, as a sentence ending. */
export const notStoredText = (reason) => NOT_STORED[reason] || 'it was not stored';

const formatSize = (bytes) => {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

const AttachmentText = ({ attachment }) => {
  const title = `${attachment.Title || ''}`.trim();
  const summary = `${attachment.Summary || ''}`.trim();
  if (!title && !summary) return null;
  return (
    <div className="chat-attachment-text">
      {title ? <strong className="chat-attachment-title">{title}</strong> : null}
      {summary ? <span className="chat-attachment-summary">{summary}</span> : null}
      <Box variant="small" color="text-status-inactive">
        AI-generated from the attachment
      </Box>
    </div>
  );
};

AttachmentText.propTypes = {
  attachment: PropTypes.shape({ Title: PropTypes.string, Summary: PropTypes.string }).isRequired,
};

/** One attachment of a chat line. */
export const ChatAttachment = ({ attachment, onOpen }) => {
  const name = `${attachment.FileName || ''}`.trim() || 'attachment';
  if (attachment.Status !== 'STORED') {
    return (
      <div className="chat-attachment" data-testid="chat-attachment">
        <span className="chat-attachment-chip">
          <Icon name="file" variant="subtle" /> <span>{name}</span>
        </span>
        <Box variant="small" color="text-status-inactive">
          Shared in the chat; not stored, because {notStoredText(attachment.NotStoredReason)}.
        </Box>
      </div>
    );
  }
  const open = () => onOpen(attachment.AttachmentId);
  const size = formatSize(attachment.Size);
  return (
    <div className="chat-attachment" data-testid="chat-attachment">
      {attachment.PreviewUrl ? (
        <button type="button" className="chat-attachment-thumb-button" onClick={open} aria-label={`Download ${name}`}>
          <img
            className="chat-attachment-thumb"
            src={attachment.PreviewUrl}
            alt={attachment.Title || name}
            loading="lazy"
          />
        </button>
      ) : null}
      <Button variant="inline-link" iconName="download" ariaLabel={`Download ${name}`} onClick={open}>
        {name}
      </Button>
      {size ? <span className="chat-attachment-size"> {size}</span> : null}
      <AttachmentText attachment={attachment} />
    </div>
  );
};

ChatAttachment.propTypes = {
  attachment: PropTypes.shape({
    AttachmentId: PropTypes.string,
    FileName: PropTypes.string,
    Status: PropTypes.string,
    NotStoredReason: PropTypes.string,
    Size: PropTypes.number,
    PreviewUrl: PropTypes.string,
    Title: PropTypes.string,
    Summary: PropTypes.string,
  }).isRequired,
  onOpen: PropTypes.func.isRequired,
};

/** The attachments of the chat line `segmentId`, from ChatAttachmentsContext. */
const ChatAttachments = ({ segmentId = '' }) => {
  const context = useContext(ChatAttachmentsContext);
  const attachments = (context?.bySegment || {})[segmentId] || [];
  if (!attachments.length) return null;
  return (
    <div className="chat-attachments">
      {attachments.map((attachment) => (
        <ChatAttachment key={attachment.AttachmentId} attachment={attachment} onOpen={context.open} />
      ))}
    </div>
  );
};

ChatAttachments.propTypes = {
  segmentId: PropTypes.string,
};

export default ChatAttachments;
