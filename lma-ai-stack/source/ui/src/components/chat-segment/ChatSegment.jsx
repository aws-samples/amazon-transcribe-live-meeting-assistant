/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A message typed in the meeting chat, as a line of the meeting's transcript:
 * on the meeting page and in the embedded transcript.
 *
 * The message is shown as it was typed, as React text: never rendered as
 * markdown or HTML, so nothing in it is formatted, and no address in it
 * becomes a link to follow. Each web address (see common/chat-links.js) is
 * shown as code with a button that copies it, so a reader who wants it can
 * take it and decide where to paste it.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Button, CopyToClipboard, Icon } from '@cloudscape-design/components';
import { splitChatText } from '../common/chat-links';
import { matchRanges, splitByRanges } from '../common/transcript-search';
import './ChatSegment.css';

/** Who a chat message is shown under when the meeting's chat named nobody. */
export const UNKNOWN_CHAT_SENDER = 'Unknown sender';

/** The sender of a chat message, as shown. */
export const chatSender = (segment) => `${segment?.speaker ?? ''}`.trim() || UNKNOWN_CHAT_SENDER;

// A run of text with the search's words marked, as plain text.
const Marked = ({ text, ranges, offset }) =>
  splitByRanges(text, ranges, offset).map((run, i) =>
    run.match ? (
      // eslint-disable-next-line react/no-array-index-key
      <mark key={i} className="transcript-search-match">
        {run.text}
      </mark>
    ) : (
      // eslint-disable-next-line react/no-array-index-key
      <React.Fragment key={i}>{run.text}</React.Fragment>
    ),
  );

/**
 * The text of a chat message: as typed, line breaks kept, its addresses as
 * code with a copy button, and the words of `query` marked.
 */
export const ChatSegmentContent = ({ text = '', query = '' }) => {
  const source = typeof text === 'string' ? text : '';
  const ranges = query ? matchRanges(source, query) : [];
  return (
    <span className="chat-segment-text">
      {splitChatText(source).map((piece) =>
        piece.kind === 'link' ? (
          <span key={piece.start} className="chat-segment-link">
            <CopyToClipboard
              variant="inline"
              textToCopy={piece.text}
              textToDisplay={
                <code>
                  <Marked text={piece.text} ranges={ranges} offset={piece.start} />
                </code>
              }
              copyButtonAriaLabel={`Copy ${piece.text}`}
              copySuccessText="Address copied"
              copyErrorText="The address could not be copied"
            />
          </span>
        ) : (
          <Marked key={piece.start} text={piece.text} ranges={ranges} offset={piece.start} />
        ),
      )}
    </span>
  );
};

ChatSegmentContent.propTypes = {
  text: PropTypes.string,
  query: PropTypes.string,
};

/**
 * A chat message as a transcript line: a chat icon, "<sender> · chat", the
 * time on the transcript's clock at which it was read from the meeting, and
 * the message. With `onSeek`, the time plays the recording from there.
 * `translation`, when there is one, is shown under the message the same way.
 */
const ChatSegment = ({ segment, time, onSeek = null, query = '', translation = '' }) => {
  const sender = chatSender(segment);
  return (
    <div className="chat-segment" data-testid="chat-segment">
      <div className="chat-segment-header">
        <span className="chat-segment-icon">
          <Icon name="contact" variant="subtle" />
        </span>
        <span className="chat-segment-label">
          <strong>{sender}</strong>
          <span className="chat-segment-kind"> · chat</span>
        </span>
        {onSeek ? (
          <Button variant="inline-link" ariaLabel={`Play from ${time}`} onClick={() => onSeek(segment.startTime)}>
            {time}
          </Button>
        ) : (
          <span className="chat-segment-time">{time}</span>
        )}
      </div>
      <ChatSegmentContent text={segment.transcript} query={query} />
      {translation ? (
        <div className="translated-text">
          <ChatSegmentContent text={translation} />
        </div>
      ) : null}
    </div>
  );
};

ChatSegment.propTypes = {
  segment: PropTypes.shape({
    speaker: PropTypes.string,
    transcript: PropTypes.string,
    startTime: PropTypes.number,
  }).isRequired,
  time: PropTypes.string.isRequired,
  onSeek: PropTypes.func,
  query: PropTypes.string,
  translation: PropTypes.string,
};

export default ChatSegment;
