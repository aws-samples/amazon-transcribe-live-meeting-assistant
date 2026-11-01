/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting summary as markdown, with its pictures of the shared screen.
 *
 * The "Slides presented" section refers to each key frame as
 * `![Slide at 12:30](keyframe:<FrameId>)`. That reference is not a link anyone
 * can fetch: it is resolved here, for this meeting only, through
 * getVideoKeyframes -- the same access check as the meeting page's strip -- and
 * shown as the frame's thumbnail, which opens the full-size image. A frame that
 * cannot be read (expired with the meeting's retention, or never stored) shows
 * as its alt text. See docs/meeting-video.md.
 *
 * Everything else renders as every other summary surface does: the shared
 * rehype chain, with the one addition that an image's src may use the
 * `keyframe:` scheme.
 */
import React, { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import { ConsoleLogger } from 'aws-amplify/utils';

import { loadKeyframe } from '../../hooks/use-chat-keyframe-links';
import './SummaryMarkdown.css';

const logger = new ConsoleLogger('SummaryMarkdown');

const KEYFRAME_SCHEME = 'keyframe:';
const FRAME_ID = /^[A-Za-z0-9_-]{1,64}$/;

/** The shared chain's schema, allowing `keyframe:` as an image source and nowhere else. */
export const summarySchema = {
  ...defaultSchema,
  protocols: { ...defaultSchema.protocols, src: [...(defaultSchema.protocols?.src || []), 'keyframe'] },
};

export const summaryRehypePlugins = [rehypeRaw, [rehypeSanitize, summarySchema]];

/** Keeps a `keyframe:` image source as it is; every other URL is treated as react-markdown treats it. */
export const summaryUrlTransform = (url, key, node) => {
  if (key === 'src' && node?.tagName === 'img' && typeof url === 'string' && url.startsWith(KEYFRAME_SCHEME)) {
    return FRAME_ID.test(url.slice(KEYFRAME_SCHEME.length)) ? url : '';
  }
  return defaultUrlTransform(url);
};

export const KeyframeImage = ({ callId, frameId, alt = '', loadFrame = loadKeyframe }) => {
  const [frame, setFrame] = useState(null);
  const [failed, setFailed] = useState(false);
  // One fresh read when the shown link stops working (a signed link lasts an hour).
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    setFailed(false);
    loadFrame(callId, frameId)
      .then((loaded) => {
        if (!current) return;
        if (loaded?.ThumbnailSignedUrl || loaded?.ImageSignedUrl) setFrame(loaded);
        else setFailed(true);
      })
      .catch((error) => {
        logger.debug('could not read a key frame for the summary', error);
        if (current) setFailed(true);
      });
    return () => {
      current = false;
    };
  }, [callId, frameId, attempt, loadFrame]);

  if (failed || !frame) {
    return (
      <span className="summary-keyframe-text" data-keyframe-id={frameId}>
        {alt || 'Shared screen'}
      </span>
    );
  }
  return (
    <a
      className="summary-keyframe"
      href={frame.ImageSignedUrl || frame.ThumbnailSignedUrl}
      target="_blank"
      rel="noopener noreferrer"
      title="Open full size"
    >
      <img
        src={frame.ThumbnailSignedUrl || frame.ImageSignedUrl}
        alt={alt || 'Shared screen'}
        loading="lazy"
        onError={() => {
          if (attempt === 0) setAttempt(1);
          else setFailed(true);
        }}
      />
    </a>
  );
};

KeyframeImage.propTypes = {
  callId: PropTypes.string.isRequired,
  frameId: PropTypes.string.isRequired,
  alt: PropTypes.string,
  loadFrame: PropTypes.func,
};

/**
 * react-markdown's components for one meeting. Built once per meeting (see the
 * useMemo below), so a re-render keeps each picture's component, and with it
 * the frame already read.
 */
const componentsFor = (callId, loadFrame) => {
  // react-markdown's own props: its hast `node`, which is not a DOM attribute, and
  // the element's attributes.
  /* eslint-disable react/prop-types */
  // eslint-disable-next-line no-unused-vars
  const SummaryImage = ({ node, src, alt, ...rest }) => {
    if (typeof src === 'string' && src.startsWith(KEYFRAME_SCHEME)) {
      const frameId = src.slice(KEYFRAME_SCHEME.length);
      if (!callId || !FRAME_ID.test(frameId)) return <span className="summary-keyframe-text">{alt}</span>;
      return <KeyframeImage callId={callId} frameId={frameId} alt={alt} loadFrame={loadFrame} />;
    }
    // eslint-disable-next-line jsx-a11y/alt-text, react/jsx-props-no-spreading
    return <img src={src} alt={alt} {...rest} />;
  };
  /* eslint-enable react/prop-types */
  return { img: SummaryImage };
};

const SummaryMarkdown = ({ callId = '', children = '', loadFrame = loadKeyframe }) => {
  const components = useMemo(() => componentsFor(callId, loadFrame), [callId, loadFrame]);
  return (
    <ReactMarkdown rehypePlugins={summaryRehypePlugins} urlTransform={summaryUrlTransform} components={components}>
      {children}
    </ReactMarkdown>
  );
};

SummaryMarkdown.propTypes = {
  callId: PropTypes.string,
  children: PropTypes.string,
  loadFrame: PropTypes.func,
};

export default SummaryMarkdown;
