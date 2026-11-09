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
 * the meeting has a recording, it also moves the page's player there (without
 * playing: `onPlayFrom(frame)`), and "Play recording from here" plays it
 * (`onPlayFrom(frame, { reveal: true })`).
 * While the meeting runs, new frames arrive through the onAddVideoKeyframe
 * subscription.
 *
 * Frames are loaded a page at a time: the first page when the page opens, the
 * next when the strip is scrolled to its end. The images are signed links
 * from getVideoKeyframes that last an hour, so a thumbnail that fails to load
 * reloads the frames, at most once a minute.
 *
 * A frame is labelled with its Title, read with its text, or else, when it
 * begins one of the meeting's chapters (from its summary), with the chapter;
 * **Export slides (PDF)** prints every frame as a deck (common/slide-deck.js).
 * Frames of the meeting's camera video rather than shared content
 * (ContentKind CAMERA) are loaded, for the clock anchors, but not shown,
 * counted, labelled or exported. Frames that repeat an earlier one
 * (DuplicateOf: the same screen, by its text) are hidden and not counted until
 * "Show all N frames" is selected, and are never exported. A frame shown
 * before its text was read is classified when the frames are next read; while
 * the meeting runs, the newest frames still without their text are read again
 * with each new frame.
 *
 * On the meeting page the strip is the Screen share tab. `onFramesChange` tells
 * the page how many frames it has loaded (which is what decides whether the
 * tab is offered at all), and `selectRequest` ({ frameId, at }) selects a frame
 * from elsewhere -- a slide named in the summary -- reading it by id when its
 * page has not been loaded yet. "Show in transcript" passes `{ reveal: true }`
 * to onShowInTranscript, asking for the transcript to be shown; selecting a
 * frame does not, so browsing the strip leaves the page where it is.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Box, Button, Container, Header, SpaceBetween, Spinner, TextFilter } from '@cloudscape-design/components';

import getVideoKeyframes from '../../graphql/queries/getVideoKeyframes';
import onAddVideoKeyframe from '../../graphql/queries/onAddVideoKeyframe';
import {
  formatFrameTime,
  frameLabel,
  frameMatches,
  matchSnippet,
  isRepeatedFrame,
  mergeKeyframes,
  newFramesCursor,
  showsSharedContent,
} from '../common/video-keyframes';
import { chapterTitlesByFrame } from '../common/chapters';
import { clockAnchorsFromFrames } from '../meeting-player/playback-sync';
import { openDeckTab as defaultOpenDeckTab, printDeck as defaultPrintDeck } from '../common/slide-deck';
import './VideoTimeline.css';

const logger = new ConsoleLogger('VideoTimeline');

export const PAGE_SIZE = 100;
// The resolver's largest page, for reading every frame at once for the deck.
const EXPORT_PAGE_SIZE = 500;
// A meeting keeps at most 1,000 frames; the bound stops a looping token.
const MAX_EXPORT_PAGES = 10;
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
  chapters = [],
  onClockAnchors = undefined,
  onFramesChange = undefined,
  selectRequest = null,
  title = '',
  loadFrames = defaultLoadFrames,
  subscribe = defaultSubscribe,
  openDeckTab = defaultOpenDeckTab,
  printDeck = defaultPrintDeck,
}) => {
  const [frames, setFrames] = useState([]);
  const [nextToken, setNextToken] = useState(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState(null);
  // Whether frames that repeat an earlier one are shown.
  const [showRepeats, setShowRepeats] = useState(false);
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
        // eslint-disable-next-line no-use-before-define
        loadPage({ after: newFramesCursor(framesRef.current) });
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
      loadPage({ after: newFramesCursor(framesRef.current) });
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
    setShowRepeats(false);
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
  // after the last one are fetched (with their signed links), from before the
  // newest frames whose text was not read yet when they were loaded, so they
  // come back read (and marked when they repeat a frame); before that the
  // paging reaches them in order.
  useEffect(() => {
    if (!isLive || !callId) return undefined;
    return subscribe(
      callId,
      () => {
        if (nextTokenRef.current) return;
        // '' rather than null with no frames yet: still a fetch of new frames.
        loadPage({ after: newFramesCursor(framesRef.current) });
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

  // The transcript's clock against the video recording's, from every loaded
  // frame that carries both, so the page's player can place a transcript time
  // (a segment, a chapter) in the recording. See meeting-player/playback-sync.js.
  useEffect(() => {
    if (onClockAnchors) onClockAnchors(clockAnchorsFromFrames(frames));
  }, [frames, onClockAnchors]);

  // The frames of shared content, and of those the ones that do not repeat an
  // earlier frame: what the strip shows and counts by default.
  const sharedFrames = useMemo(() => frames.filter(showsSharedContent), [frames]);
  const distinctCount = useMemo(() => sharedFrames.filter((f) => !isRepeatedFrame(f)).length, [sharedFrames]);
  const repeatCount = sharedFrames.length - distinctCount;

  // How many frames are shown, whether there are more, and whether the first
  // page has been read (`loaded`), for the page's tab label and its player.
  useEffect(() => {
    if (onFramesChange) onFramesChange({ count: distinctCount, more: Boolean(nextToken), loaded });
  }, [distinctCount, nextToken, loaded, onFramesChange]);

  // A frame selected from elsewhere: shown large, and its thumbnail brought into
  // view. Read by id when it is on a page not loaded yet.
  const revealSelectedRef = useRef(false);
  useEffect(() => {
    const frameId = selectRequest?.frameId;
    if (!frameId) return undefined;
    if (framesRef.current.some((f) => f.FrameId === frameId)) {
      revealSelectedRef.current = true;
      setSelectedId(frameId);
      return undefined;
    }
    const generation = generationRef.current;
    let current = true;
    loadFrames({ callId, frameId })
      .then((page) => {
        if (!current || generation !== generationRef.current) return;
        const incoming = (page?.VideoKeyframes || []).filter((f) => f?.FrameId === frameId);
        if (!incoming.length) return;
        framesRef.current = mergeKeyframes(framesRef.current, incoming);
        setFrames(framesRef.current);
        revealSelectedRef.current = true;
        setSelectedId(frameId);
      })
      .catch((e) => logger.debug('could not read the requested key frame', e));
    return () => {
      current = false;
    };
  }, [selectRequest]);

  useEffect(() => {
    if (!revealSelectedRef.current || !selectedId) return;
    revealSelectedRef.current = false;
    const button = stripRef.current?.querySelector(`[data-frame-id="${selectedId}"]`);
    if (button?.scrollIntoView) button.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
  }, [selectedId, frames]);

  /**
   * Every frame, read fresh (for links that last the print), then the deck. The
   * tab is opened first, within the click: a browser blocks one opened after
   * the frames have been awaited.
   */
  const exportDeck = async () => {
    setExportError(null);
    const tab = openDeckTab();
    if (!tab) {
      setExportError('The browser blocked the new tab. Allow pop-ups for this site and try again.');
      return;
    }
    setExporting(true);
    try {
      let all = [];
      let token = null;
      let pages = 0;
      do {
        // eslint-disable-next-line no-await-in-loop
        const page = await loadFrames({ callId, limit: EXPORT_PAGE_SIZE, nextToken: token, afterFrameId: null });
        const incoming = page?.VideoKeyframes || [];
        all = mergeKeyframes(all, incoming);
        token = (incoming.length && page?.nextToken) || null;
        pages += 1;
      } while (token && pages < MAX_EXPORT_PAGES);
      await printDeck(tab, { title: title || callId, frames: all, chapters });
    } catch (e) {
      logger.error('could not export the key frames', e);
      tab.close();
      setExportError('The key frames could not be exported.');
    } finally {
      setExporting(false);
    }
  };

  const select = (frame) => {
    setSelectedId(frame.FrameId);
    if (onShowInTranscript) onShowInTranscript(frame.Timestamp);
    if (canPlayRecording && onPlayFrom) onPlayFrom(frame);
  };

  const selected = frames.find((f) => f.FrameId === selectedId);
  const searching = Boolean(search.trim());
  // Repeats are hidden unless asked for; a repeat selected from elsewhere (a
  // slide the summary names) is shown so its thumbnail can be found.
  const visibleFrames = useMemo(
    () => (showRepeats ? sharedFrames : sharedFrames.filter((f) => !isRepeatedFrame(f) || f.FrameId === selectedId)),
    [sharedFrames, showRepeats, selectedId],
  );
  const shown = useMemo(
    () => (search.trim() ? visibleFrames.filter((f) => frameMatches(f, search)) : visibleFrames),
    [visibleFrames, search],
  );
  // Labelled from the frames shown, so a chapter is not given to a hidden repeat.
  const chapterTitles = useMemo(() => chapterTitlesByFrame(chapters, visibleFrames), [chapters, visibleFrames]);

  // A search covers the whole meeting: the pages not loaded yet are loaded,
  // one after another, while it is typed. So do pages that held only camera
  // frames, which would otherwise leave nothing to scroll to the next page
  // with. A page that fails stops it; Load more tries again.
  const nothingShown = !sharedFrames.length;
  useEffect(() => {
    if ((searching || nothingShown) && nextToken && !loading && !error) loadPage({ token: nextToken });
  }, [searching, nothingShown, nextToken, loading, error, loadPage]);

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

  if (loaded && !sharedFrames.length && !error) {
    // Nothing to show: a meeting without video, before its first frame, or
    // with only camera frames so far.
    return null;
  }
  if (!loaded) return null;

  return (
    <Container
      header={
        <Header
          variant="h4"
          counter={`(${distinctCount}${nextToken ? '+' : ''})`}
          description="The shared screen each time it changed. Select one to see it full size."
          actions={
            <Button iconName="download" loading={exporting} onClick={exportDeck}>
              Export slides (PDF)
            </Button>
          }
        >
          Shared screen
        </Header>
      }
    >
      <SpaceBetween size="s">
        {error && <Box color="text-status-error">{error}</Box>}
        {exportError && <Box color="text-status-error">{exportError}</Box>}
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
        {repeatCount > 0 && (
          <SpaceBetween size="xs" direction="horizontal">
            {!showRepeats && (
              <Box variant="small" color="text-body-secondary">
                {`${repeatCount} ${repeatCount === 1 ? 'frame repeats' : 'frames repeat'} the screen before ${
                  repeatCount === 1 ? 'it' : 'them'
                } and ${repeatCount === 1 ? 'is' : 'are'} hidden.`}
              </Box>
            )}
            <Button variant="inline-link" onClick={() => setShowRepeats((v) => !v)}>
              {showRepeats ? 'Hide repeated frames' : `Show all ${sharedFrames.length} frames`}
            </Button>
          </SpaceBetween>
        )}
        {searching && !shown.length && !loading && !nextToken && (
          <Box color="text-body-secondary">No frame shows that text.</Box>
        )}
        <div className="video-timeline-strip" ref={stripRef} onScroll={onStripScroll} data-testid="video-timeline">
          {shown.map((frame) => {
            const label = frameLabel(frame, chapterTitles);
            return (
              <button
                type="button"
                key={frame.FrameId}
                data-frame-id={frame.FrameId}
                className={`video-timeline-frame${
                  frame.FrameId === selectedId ? ' video-timeline-frame-selected' : ''
                }`}
                onClick={() => select(frame)}
                aria-pressed={frame.FrameId === selectedId}
                title={`Key frame at ${formatFrameTime(frame.Timestamp)}`}
              >
                {label && <span className="video-timeline-chapter">{label}</span>}
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
            );
          })}
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
                <Button variant="inline-link" onClick={() => onShowInTranscript(selected.Timestamp, { reveal: true })}>
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
  chapters: PropTypes.arrayOf(PropTypes.shape({ seconds: PropTypes.number, title: PropTypes.string })),
  onClockAnchors: PropTypes.func,
  onFramesChange: PropTypes.func,
  selectRequest: PropTypes.shape({ frameId: PropTypes.string, at: PropTypes.number }),
  title: PropTypes.string,
  loadFrames: PropTypes.func,
  subscribe: PropTypes.func,
  openDeckTab: PropTypes.func,
  printDeck: PropTypes.func,
};

export default VideoTimeline;
