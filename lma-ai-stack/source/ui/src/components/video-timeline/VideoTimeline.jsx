/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting's key frames as a strip of thumbnails, in time order: each slide,
 * page or view of the shared screen, with the time it went up (see
 * docs/meeting-video.md).
 *
 * Selecting a frame shows it large and moves the transcript to that time; when
 * the meeting has a video recording, it also plays the recording from there.
 * While the meeting runs, new frames arrive through the onAddVideoKeyframe
 * subscription.
 *
 * Frames are loaded a page at a time: the first page when the page opens, the
 * next when the strip is scrolled to its end. The images are signed links
 * from getVideoKeyframes that last an hour, so a thumbnail that fails to load
 * reloads the frames, at most once a minute.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Box, Button, Container, Header, SpaceBetween, Spinner, TextFilter } from '@cloudscape-design/components';

import getVideoKeyframes from '../../graphql/queries/getVideoKeyframes';
import onAddVideoKeyframe from '../../graphql/queries/onAddVideoKeyframe';
import { formatFrameTime, frameMatches, matchSnippet, mergeKeyframes } from '../common/video-keyframes';
import './VideoTimeline.css';

const logger = new ConsoleLogger('VideoTimeline');

export const PAGE_SIZE = 100;
const RELOAD_AFTER_MS = 60 * 1000;

const defaultLoadFrames = async (variables) => {
  const client = generateClient();
  const response = await client.graphql({ query: getVideoKeyframes, variables });
  return response?.data?.getVideoKeyframes || { VideoKeyframes: [], nextToken: null };
};

const defaultSubscribe = (callId, onFrame, onError) => {
  const client = generateClient();
  const subscription = client.graphql({ query: onAddVideoKeyframe, variables: { callId } }).subscribe({
    next: (message) => {
      const frame = message?.data?.onAddVideoKeyframe;
      if (frame?.CallId === callId) onFrame(frame);
    },
    error: onError,
  });
  return () => subscription.unsubscribe();
};

const VideoTimeline = ({
  callId,
  isLive = false,
  canPlayRecording = false,
  onShowInTranscript = undefined,
  onPlayFrom = undefined,
  loadFrames = defaultLoadFrames,
  subscribe = defaultSubscribe,
}) => {
  const [frames, setFrames] = useState([]);
  const [nextToken, setNextToken] = useState(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const stripRef = useRef(null);
  const sentinelRef = useRef(null);
  const framesRef = useRef(frames);
  const nextTokenRef = useRef(nextToken);
  const loadingRef = useRef(false);
  // A frame announced while a page was loading: fetch what came after once it is done.
  const pendingNewRef = useRef(false);
  // Which meeting the requests in flight belong to. A response for an earlier
  // meeting is dropped rather than merged into this one's strip.
  const generationRef = useRef(0);
  const lastReloadRef = useRef(0);
  const followRef = useRef(true);

  framesRef.current = frames;
  nextTokenRef.current = nextToken;

  /**
   * Runs `work` as the one load in flight: `fetchPage(variables)` asks for a
   * page, and `apply(page)` merges it, only while this is still the meeting the
   * load was started for.
   */
  const runLoad = useCallback(
    async (work) => {
      if (loadingRef.current) return false;
      const generation = generationRef.current;
      const current = () => generation === generationRef.current;
      loadingRef.current = true;
      setLoading(true);
      try {
        await work({
          fetchPage: (variables) => loadFrames({ callId, limit: PAGE_SIZE, ...variables }),
          apply: (merge) => {
            if (!current()) return false;
            framesRef.current = merge(framesRef.current);
            setFrames(framesRef.current);
            return true;
          },
          current,
        });
        if (current()) setError(null);
      } catch (e) {
        logger.error('could not load key frames', e);
        if (current()) setError('The key frames could not be loaded.');
      } finally {
        if (current()) {
          loadingRef.current = false;
          setLoading(false);
          setLoaded(true);
        }
      }
      return current();
    },
    [callId, loadFrames],
  );

  /** One page by token (paging), or the frames after a given one (new frames). */
  const loadPage = useCallback(
    async ({ token = null, after = null } = {}) => {
      if (loadingRef.current) {
        if (after !== null) pendingNewRef.current = true;
        return;
      }
      const ran = await runLoad(async ({ fetchPage, apply }) => {
        const page = await fetchPage({ nextToken: token, afterFrameId: after || null });
        const incoming = page?.VideoKeyframes || [];
        const applied = apply((shown) => mergeKeyframes(shown, incoming));
        // A page of frames after a given one does not move the paging cursor.
        // An empty page ends the paging: DynamoDB hands back a token for a page
        // that happened to end on the last frame, and it leads nowhere.
        if (applied && after === null) {
          nextTokenRef.current = (incoming.length && page?.nextToken) || null;
          setNextToken(nextTokenRef.current);
        }
      });
      if (ran && pendingNewRef.current && !nextTokenRef.current) {
        pendingNewRef.current = false;
        const last = framesRef.current[framesRef.current.length - 1];
        // eslint-disable-next-line no-use-before-define
        loadPage({ after: last?.FrameId || '' });
      }
    },
    [runLoad],
  );

  /**
   * Fetches again every page already shown, for fresh signed links, keeping
   * the strip as it is: the frames merge in place, and the paging cursor ends
   * where it was.
   */
  const refreshShown = useCallback(async () => {
    // Returns whether it ran: not when another load was in flight.
    const shown = framesRef.current.length;
    const ran = await runLoad(async ({ fetchPage, apply, current }) => {
      let token = null;
      let fetched = 0;
      do {
        // eslint-disable-next-line no-await-in-loop
        const page = await fetchPage({ nextToken: token, afterFrameId: null });
        const incoming = page?.VideoKeyframes || [];
        if (!apply((shownFrames) => mergeKeyframes(shownFrames, incoming))) return;
        fetched += incoming.length;
        token = page?.nextToken || null;
      } while (token && fetched < shown && current());
      nextTokenRef.current = token;
      setNextToken(token);
    });
    // A frame announced during the renewal is fetched now, as after a page.
    if (ran && pendingNewRef.current && !nextTokenRef.current) {
      pendingNewRef.current = false;
      const last = framesRef.current[framesRef.current.length - 1];
      loadPage({ after: last?.FrameId || '' });
    }
    return ran;
  }, [runLoad, loadPage]);

  // First page, and a fresh start when the meeting changes.
  useEffect(() => {
    generationRef.current += 1;
    loadingRef.current = false;
    pendingNewRef.current = false;
    framesRef.current = [];
    nextTokenRef.current = null;
    setFrames([]);
    setNextToken(null);
    setLoaded(false);
    setSelectedId(null);
    loadPage();
  }, [callId]);

  // The next page when the end of the strip comes into view.
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !nextToken || typeof IntersectionObserver === 'undefined') return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting) && nextTokenRef.current) {
          loadPage({ token: nextTokenRef.current });
        }
      },
      { root: stripRef.current, rootMargin: '0px 400px 0px 0px' },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [nextToken, loadPage]);

  // New frames while the meeting runs. Once every page is loaded, the frames
  // after the last one are fetched (with their signed links); before that the
  // paging reaches them in order.
  useEffect(() => {
    if (!isLive || !callId) return undefined;
    return subscribe(
      callId,
      () => {
        if (nextTokenRef.current) return;
        const last = framesRef.current[framesRef.current.length - 1];
        // '' rather than null with no frames yet: still a fetch of new frames.
        loadPage({ after: last?.FrameId || '' });
      },
      (e) => logger.error('key frame subscription failed', e),
    );
  }, [callId, isLive, subscribe, loadPage]);

  // Keep the newest frame in view while the strip is at its end.
  useEffect(() => {
    const strip = stripRef.current;
    if (strip && isLive && followRef.current && strip.scrollTo) {
      strip.scrollTo({ left: strip.scrollWidth, behavior: 'smooth' });
    }
  }, [frames.length, isLive]);

  const onStripScroll = () => {
    const strip = stripRef.current;
    if (strip) followRef.current = strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 40;
  };

  // A signed link has expired (or failed): renew what is shown, not too often.
  const onImageError = () => {
    if (Date.now() - lastReloadRef.current < RELOAD_AFTER_MS) return;
    // Counted from a renewal that ran: one skipped because a page was loading
    // leaves the next failing image free to try again.
    refreshShown().then((ran) => {
      if (ran) lastReloadRef.current = Date.now();
    });
  };

  const select = (frame) => {
    setSelectedId(frame.FrameId);
    if (onShowInTranscript) onShowInTranscript(frame.Timestamp);
    if (canPlayRecording && onPlayFrom) onPlayFrom(frame);
  };

  const selected = frames.find((f) => f.FrameId === selectedId);
  const searching = Boolean(search.trim());
  const shown = useMemo(
    () => (search.trim() ? frames.filter((f) => frameMatches(f, search)) : frames),
    [frames, search],
  );

  // A search covers the whole meeting: the pages not loaded yet are loaded,
  // one after another, while it is typed. A page that fails stops it; Load
  // more tries again.
  useEffect(() => {
    if (searching && nextToken && !loading && !error) loadPage({ token: nextToken });
  }, [searching, nextToken, loading, error, loadPage]);

  // A frame's text is read a few seconds after the frame is stored, so a frame
  // shown before then has none yet. Starting a search reads the frames shown
  // again, once, when any of them is still without its text.
  const searchStartedRef = useRef(false);
  useEffect(() => {
    if (!searching) {
      searchStartedRef.current = false;
      return;
    }
    if (searchStartedRef.current) return;
    searchStartedRef.current = true;
    if (framesRef.current.some((f) => typeof f.SlideText !== 'string')) refreshShown();
  }, [searching, refreshShown]);

  if (loaded && !frames.length && !error) {
    // Nothing to show: a meeting without video, or before its first frame.
    return null;
  }
  if (!loaded) return null;

  return (
    <Container
      header={
        <Header
          variant="h4"
          counter={`(${frames.length}${nextToken ? '+' : ''})`}
          description="The shared screen each time it changed. Select one to move the transcript to that time."
        >
          Shared screen
        </Header>
      }
    >
      <SpaceBetween size="s">
        {error && <Box color="text-status-error">{error}</Box>}
        <TextFilter
          filteringText={search}
          filteringPlaceholder="Find text on the shared screen"
          filteringAriaLabel="Find text on the shared screen"
          countText={
            searching
              ? `${shown.length} ${shown.length === 1 ? 'frame' : 'frames'}${nextToken ? ' so far' : ''}`
              : undefined
          }
          onChange={({ detail }) => setSearch(detail.filteringText)}
        />
        {searching && !shown.length && !loading && !nextToken && (
          <Box color="text-body-secondary">No frame shows that text.</Box>
        )}
        <div className="video-timeline-strip" ref={stripRef} onScroll={onStripScroll} data-testid="video-timeline">
          {shown.map((frame) => (
            <button
              type="button"
              key={frame.FrameId}
              className={`video-timeline-frame${frame.FrameId === selectedId ? ' video-timeline-frame-selected' : ''}`}
              onClick={() => select(frame)}
              aria-pressed={frame.FrameId === selectedId}
              title={`Key frame at ${formatFrameTime(frame.Timestamp)}`}
            >
              {frame.ThumbnailSignedUrl ? (
                <img
                  src={frame.ThumbnailSignedUrl}
                  alt={`Shared screen at ${formatFrameTime(frame.Timestamp)}`}
                  loading="lazy"
                  onError={onImageError}
                />
              ) : (
                <span className="video-timeline-missing">No image</span>
              )}
              <span className="video-timeline-time">{formatFrameTime(frame.Timestamp)}</span>
              {searching && <span className="video-timeline-snippet">{matchSnippet(frame, search)}</span>}
            </button>
          ))}
          <div ref={sentinelRef} className="video-timeline-sentinel" />
          {loading && <Spinner />}
        </div>
        {nextToken && !loading && (!searching || error) && (
          <Button variant="inline-link" onClick={() => loadPage({ token: nextToken })}>
            Load more
          </Button>
        )}
        {selected && (
          <SpaceBetween size="xs">
            <SpaceBetween size="xs" direction="horizontal">
              <Box variant="strong">{formatFrameTime(selected.Timestamp)}</Box>
              {onShowInTranscript && (
                <Button variant="inline-link" onClick={() => onShowInTranscript(selected.Timestamp)}>
                  Show in transcript
                </Button>
              )}
              {canPlayRecording && onPlayFrom && (
                <Button
                  variant="inline-link"
                  iconName="caret-right-filled"
                  onClick={() => onPlayFrom(selected, { reveal: true })}
                >
                  Play recording from here
                </Button>
              )}
              {selected.ImageSignedUrl && (
                <Button
                  variant="inline-link"
                  href={selected.ImageSignedUrl}
                  target="_blank"
                  iconName="external"
                  iconAlign="right"
                >
                  Open full size
                </Button>
              )}
            </SpaceBetween>
            {selected.SlideText && (
              <Box variant="small" color="text-body-secondary">
                <span className="video-timeline-slide-text">{selected.SlideText}</span>
              </Box>
            )}
            {selected.ImageSignedUrl && (
              <img
                className="video-timeline-preview"
                src={selected.ImageSignedUrl}
                alt={`Shared screen at ${formatFrameTime(selected.Timestamp)}, full size`}
                onError={onImageError}
              />
            )}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
};

VideoTimeline.propTypes = {
  callId: PropTypes.string.isRequired,
  isLive: PropTypes.bool,
  canPlayRecording: PropTypes.bool,
  onShowInTranscript: PropTypes.func,
  onPlayFrom: PropTypes.func,
  loadFrames: PropTypes.func,
  subscribe: PropTypes.func,
};

export default VideoTimeline;
