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
 * shown as the frame's thumbnail, which opens the full-size image. Every frame
 * the summary names is read in one request (`loadFrames`), not one request per
 * slide; a frame whose link has lapsed is read again by itself (`loadFrame`).
 * A frame that cannot be read (expired with the meeting's retention, or never
 * stored) shows as its alt text. See docs/meeting-video.md.
 *
 * Everything else renders as every other summary surface does: the shared
 * rehype chain, with the one addition that an image's src may use the
 * `keyframe:` scheme.
 */
import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import { ConsoleLogger } from 'aws-amplify/utils';

import { FRAME_ID, loadKeyframe, loadKeyframes } from '../../hooks/use-chat-keyframe-links';
import './SummaryMarkdown.css';

const logger = new ConsoleLogger('SummaryMarkdown');

const KEYFRAME_SCHEME = 'keyframe:';
// A picture whose source is a frame, in the two forms the summary prompt and
// the shared chain produce: markdown image syntax and an <img> tag. A picture in
// any other form SummaryImage renders is not found here, and reads its own frame.
const FRAME_REFERENCE =
  /!\[[^\]]*\]\(keyframe:([A-Za-z0-9_-]{1,64})\)|<img\b[^>]*\bsrc=["']keyframe:([A-Za-z0-9_-]{1,64})["']/g;

/** The frame ids a summary's pictures refer to, each once, in the order they first appear. */
export const frameIdsIn = (markdown) =>
  typeof markdown === 'string'
    ? [...new Set([...markdown.matchAll(FRAME_REFERENCE)].map((match) => match[1] || match[2]))]
    : [];

/**
 * The summary's read of its frames: `requested`, the ids it asks for, and
 * `frames`, what it has read, by id. A picture whose id was not requested reads
 * its own frame. Absent when a KeyframeImage is used on its own, which then
 * reads its own frame too.
 */
const SummaryFrames = createContext(null);

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

const hasLink = (frame) => Boolean(frame?.ThumbnailSignedUrl || frame?.ImageSignedUrl);

export const KeyframeImage = ({ callId, frameId, alt = '', loadFrame = loadKeyframe }) => {
  const batch = useContext(SummaryFrames);
  const inBatch = Boolean(batch?.requested.has(frameId));
  // A frame read by itself: on its own, or again after its link stopped working.
  const [ownFrame, setOwnFrame] = useState(null);
  const [failed, setFailed] = useState(false);
  // One fresh read when the shown link stops working (a signed link lasts an hour).
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // Inside a summary, the first read of a frame it asked for is the summary's,
    // of every frame at once.
    if (inBatch && attempt === 0) return undefined;
    let current = true;
    setFailed(false);
    loadFrame(callId, frameId)
      .then((loaded) => {
        if (!current) return;
        if (hasLink(loaded)) setOwnFrame(loaded);
        else setFailed(true);
      })
      .catch((error) => {
        logger.debug('could not read a key frame for the summary', error);
        if (current) setFailed(true);
      });
    return () => {
      current = false;
    };
  }, [inBatch, callId, frameId, attempt, loadFrame]);

  const batchFrame = inBatch && attempt === 0 ? batch.frames[frameId] : undefined;
  const frame = ownFrame || (hasLink(batchFrame) ? batchFrame : null);
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
 * useMemo below), so a re-render keeps each picture's component; the frames
 * themselves are held by the summary's read (SummaryFrames), and a re-read after
 * a lapsed link by the picture.
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

const SummaryMarkdown = ({ callId = '', children = '', loadFrame = loadKeyframe, loadFrames = loadKeyframes }) => {
  const components = useMemo(() => componentsFor(callId, loadFrame), [callId, loadFrame]);
  const frameIds = useMemo(() => frameIdsIn(children).filter((id) => FRAME_ID.test(id)), [children]);
  const frameKey = frameIds.join(' ');
  const [frames, setFrames] = useState({});

  // Another meeting's frames are never shown for this one.
  useEffect(() => {
    setFrames({});
  }, [callId]);

  // A changed summary (regenerated or edited) keeps showing the frames already
  // read while the ones it now names are read, and the answer is added to them.
  useEffect(() => {
    let current = true;
    if (!callId || frameIds.length === 0) return undefined;
    loadFrames(callId, frameIds)
      .then((loaded) => {
        if (current) setFrames((previous) => ({ ...previous, ...(loaded || {}) }));
      })
      .catch((error) => {
        logger.debug('could not read the key frames for the summary', error);
      });
    return () => {
      current = false;
    };
    // frameKey stands for frameIds, so a re-render of the same summary reads nothing again.
  }, [callId, frameKey, loadFrames]);

  const batch = useMemo(() => ({ requested: new Set(frameIds), frames }), [frameKey, frames]);

  return (
    <SummaryFrames.Provider value={batch}>
      <ReactMarkdown rehypePlugins={summaryRehypePlugins} urlTransform={summaryUrlTransform} components={components}>
        {children}
      </ReactMarkdown>
    </SummaryFrames.Provider>
  );
};

SummaryMarkdown.propTypes = {
  callId: PropTypes.string,
  children: PropTypes.string,
  loadFrame: PropTypes.func,
  loadFrames: PropTypes.func,
};

export default SummaryMarkdown;
