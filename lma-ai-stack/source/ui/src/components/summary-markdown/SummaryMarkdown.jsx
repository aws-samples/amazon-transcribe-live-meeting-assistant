/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting summary as markdown, with its pictures of the shared screen.
 *
 * The screen share summary section (SCREEN SHARE SUMMARY, or SLIDES PRESENTED
 * in a summary written before it was renamed) refers to each key frame as
 * `![Slide at 12:30](keyframe:<FrameId>)`; nothing here depends on the section's
 * title, so a picture in any section, or a summary of either age, is shown the
 * same way. That reference is not a link anyone
 * can fetch: it is resolved here, for this meeting only, through
 * getVideoKeyframes -- the same access check as the meeting page's strip -- and
 * shown as the frame's thumbnail, which opens the full-size image. Every frame
 * the summary names is read in one request (`loadFrames`), not one request per
 * slide; a frame whose link has lapsed is read again by itself (`loadFrame`).
 * A frame that cannot be read (expired with the meeting's retention, or never
 * stored) shows as its alt text. See docs/meeting-video.md.
 *
 * The meeting page's Overview tab asks for the pictures differently
 * (`keyframes`): as a link that opens the frame on the page's Screen share tab
 * ('link'), or as their alt text alone ('text', for a meeting whose frames are
 * gone), so that a summary of a long presentation is not a column of slides.
 * Neither reads any frame.
 *
 * Everything else renders as every other summary surface does, through the
 * shared rehype chain, with one difference: the only picture a summary shows is
 * a `keyframe:` one. Any other image, in any section, is shown as its alt text,
 * so a summary never makes the browser fetch from an address its text names.
 */
import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Button } from '@cloudscape-design/components';

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

/**
 * The shared chain's schema, with `keyframe:` as the only image source. A
 * source with no scheme at all passes a protocol check, so SummaryImage and
 * summaryUrlTransform refuse it as well; <picture> and <source> are left out
 * because a srcset is not checked for a protocol.
 */
export const summarySchema = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames || []).filter((name) => name !== 'picture' && name !== 'source'),
  protocols: { ...defaultSchema.protocols, src: ['keyframe'] },
};

export const summaryRehypePlugins = [rehypeRaw, [rehypeSanitize, summarySchema]];

/** Keeps a `keyframe:` image source as it is and drops every other one; other URLs as react-markdown treats them. */
export const summaryUrlTransform = (url, key, node) => {
  if (key === 'src' && node?.tagName === 'img') {
    const isFrame = typeof url === 'string' && url.startsWith(KEYFRAME_SCHEME);
    return isFrame && FRAME_ID.test(url.slice(KEYFRAME_SCHEME.length)) ? url : '';
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
const componentsFor = (callId, loadFrame, keyframes, onKeyframeSelect) => {
  // react-markdown's own props: its hast `node`, which is not a DOM attribute, and
  // the element's attributes.
  /* eslint-disable react/prop-types */
  // eslint-disable-next-line no-unused-vars
  const SummaryImage = ({ node, src, alt }) => {
    if (typeof src === 'string' && src.startsWith(KEYFRAME_SCHEME)) {
      const frameId = src.slice(KEYFRAME_SCHEME.length);
      if (!callId || !FRAME_ID.test(frameId) || keyframes === 'text') {
        return <span className="summary-keyframe-text">{alt}</span>;
      }
      if (keyframes === 'link') {
        return (
          <span className="summary-keyframe-link" data-keyframe-id={frameId}>
            <Button
              variant="inline-link"
              iconName="file-open"
              ariaLabel={`${alt || 'Shared screen'}: show it on the Screen share tab`}
              onClick={() => onKeyframeSelect?.(frameId)}
            >
              {alt || 'Shared screen'}
            </Button>
          </span>
        );
      }
      return <KeyframeImage callId={callId} frameId={frameId} alt={alt} loadFrame={loadFrame} />;
    }
    // Not a frame of this meeting: never fetched, whatever its address.
    return alt ? <span>{alt}</span> : null;
  };
  /* eslint-enable react/prop-types */
  return { img: SummaryImage };
};

const SummaryMarkdown = ({
  callId = '',
  children = '',
  loadFrame = loadKeyframe,
  loadFrames = loadKeyframes,
  keyframes = 'image',
  onKeyframeSelect = undefined,
}) => {
  const components = useMemo(
    () => componentsFor(callId, loadFrame, keyframes, onKeyframeSelect),
    [callId, loadFrame, keyframes, onKeyframeSelect],
  );
  // Only pictures are read; a link or a caption needs no frame.
  const frameIds = useMemo(
    () => (keyframes === 'image' ? frameIdsIn(children).filter((id) => FRAME_ID.test(id)) : []),
    [children, keyframes],
  );
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
  // How a key frame is shown: as the picture, as a link to the Screen share tab, or as its alt text.
  keyframes: PropTypes.oneOf(['image', 'link', 'text']),
  // Called with the FrameId when a 'link' is followed.
  onKeyframeSelect: PropTypes.func,
};

export default SummaryMarkdown;
