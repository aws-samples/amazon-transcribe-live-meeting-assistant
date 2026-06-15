/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useState, useCallback, useRef, useEffect } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

const logger = new ConsoleLogger('useVideoSync');

/**
 * useVideoSync — Custom hook for bidirectional video ↔ transcript synchronization.
 *
 * Handles two sync directions:
 *   A) Video playback → Highlight active transcript segment (+ auto-scroll)
 *   B) Click transcript segment → Seek video to segment start time
 *
 * @param {Object} options
 * @param {Array} options.segments — Transcript segments array, each with:
 *   { startTime: number (seconds), endTime: number (seconds), segmentId: string, ... }
 * @param {React.RefObject} options.videoPlayerRef — Ref to the VideoPlayer imperative handle
 * @param {number} [options.timeOffset=0] — Offset in seconds between video 0:00 and
 *   transcript timestamps (video 0:00 = firstSegment.startTime - offset).
 *   If 0, assumes video start aligns with first transcript segment.
 * @param {boolean} [options.autoScroll=true] — Whether to auto-scroll to active segment
 * @param {number} [options.scrollBehavior='smooth'] — CSS scroll behavior
 *
 * @returns {Object}
 *   activeSegmentIndex — Index of the currently active segment (-1 if none)
 *   activeSegmentId — ID of the active segment (null if none)
 *   seekToSegment — Function: (segmentIndex) => seeks video to that segment's start
 *   seekToTime — Function: (timeInSeconds) => seeks video to absolute time
 *   isPlaying — Whether video is currently playing
 *   currentTime — Current playback time (in transcript time-space)
 *   handleTimeUpdate — Callback to pass to VideoPlayer's onTimeUpdate prop
 *   getSegmentClassName — Helper: returns CSS class string for a segment by index
 */
const useVideoSync = ({
  segments = [],
  videoPlayerRef,
  timeOffset: timeOffsetProp,
  autoScroll = true,
  scrollBehavior = 'smooth',
} = {}) => {
  const [activeSegmentIndex, setActiveSegmentIndex] = useState(-1);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const lastScrolledIndex = useRef(-1);
  const segmentRefs = useRef(new Map());

  // Calculate effective time offset:
  // If not explicitly provided, derive from first segment's startTime
  // (video 0:00 corresponds to the start of transcription)
  const getTimeOffset = useCallback(() => {
    if (timeOffsetProp !== undefined && timeOffsetProp !== null) {
      return timeOffsetProp;
    }
    // Default: video starts at the same time as the first transcript segment
    if (segments.length > 0 && segments[0].startTime != null) {
      return segments[0].startTime;
    }
    return 0;
  }, [timeOffsetProp, segments]);

  /**
   * Convert video playback time to transcript time-space.
   * Video currentTime + offset = transcript absolute time.
   */
  const videoTimeToTranscriptTime = useCallback((videoTime) => {
    return videoTime + getTimeOffset();
  }, [getTimeOffset]);

  /**
   * Convert transcript time to video playback time.
   */
  const transcriptTimeToVideoTime = useCallback((transcriptTime) => {
    return transcriptTime - getTimeOffset();
  }, [getTimeOffset]);

  /**
   * Binary search for the active segment at a given transcript time.
   * Returns the index of the segment whose [startTime, endTime) contains the time,
   * or -1 if no match.
   */
  const findActiveSegment = useCallback((transcriptTime) => {
    if (!segments.length) return -1;

    let low = 0;
    let high = segments.length - 1;

    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      const seg = segments[mid];

      if (transcriptTime < seg.startTime) {
        high = mid - 1;
      } else if (transcriptTime >= seg.endTime) {
        low = mid + 1;
      } else {
        // transcriptTime is within [startTime, endTime)
        return mid;
      }
    }

    // Check if we're in a gap between segments — find nearest
    // If time is past all segments, highlight the last one during its range
    if (low > 0 && low <= segments.length) {
      const prevSeg = segments[low - 1];
      // If within 0.5s of prev segment end, still highlight it (smooths transitions)
      if (transcriptTime - prevSeg.endTime < 0.5) {
        return low - 1;
      }
    }

    return -1;
  }, [segments]);

  /**
   * handleTimeUpdate — Connect this to VideoPlayer's onTimeUpdate prop.
   * Direction A: Video → Transcript highlight
   */
  const handleTimeUpdate = useCallback(({ currentTime: videoCurrentTime, duration }) => {
    const transcriptTime = videoTimeToTranscriptTime(videoCurrentTime);
    setCurrentTime(transcriptTime);
    setIsPlaying(true);

    const newIndex = findActiveSegment(transcriptTime);

    if (newIndex !== activeSegmentIndex) {
      setActiveSegmentIndex(newIndex);

      // Auto-scroll to active segment
      if (autoScroll && newIndex >= 0 && newIndex !== lastScrolledIndex.current) {
        lastScrolledIndex.current = newIndex;
        const segmentEl = segmentRefs.current.get(newIndex);
        if (segmentEl) {
          segmentEl.scrollIntoView({
            behavior: scrollBehavior,
            block: 'nearest',
            inline: 'nearest',
          });
        }
      }
    }
  }, [videoTimeToTranscriptTime, findActiveSegment, activeSegmentIndex, autoScroll, scrollBehavior]);

  /**
   * seekToSegment — Direction B: Transcript click → Video seek.
   * @param {number} segmentIndex — Index into segments array
   */
  const seekToSegment = useCallback((segmentIndex) => {
    if (segmentIndex < 0 || segmentIndex >= segments.length) {
      logger.warn('seekToSegment: index out of bounds', segmentIndex);
      return;
    }

    const segment = segments[segmentIndex];
    const videoTime = transcriptTimeToVideoTime(segment.startTime);

    logger.debug(`Seeking to segment ${segmentIndex}`, {
      segmentStartTime: segment.startTime,
      videoTime,
    });

    if (videoPlayerRef?.current) {
      videoPlayerRef.current.seek(Math.max(0, videoTime));
      // Optionally auto-play on seek
      if (videoPlayerRef.current.paused) {
        videoPlayerRef.current.play();
      }
    }

    // Immediately highlight the target segment
    setActiveSegmentIndex(segmentIndex);
  }, [segments, transcriptTimeToVideoTime, videoPlayerRef]);

  /**
   * seekToTime — Seek video to an absolute transcript timestamp.
   * @param {number} transcriptTime — Time in seconds (transcript time-space)
   */
  const seekToTime = useCallback((transcriptTime) => {
    const videoTime = transcriptTimeToVideoTime(transcriptTime);
    if (videoPlayerRef?.current) {
      videoPlayerRef.current.seek(Math.max(0, videoTime));
    }
  }, [transcriptTimeToVideoTime, videoPlayerRef]);

  /**
   * getSegmentClassName — Returns a CSS class name for styling the segment.
   * @param {number} index — Segment index
   * @returns {string} CSS class name(s)
   */
  const getSegmentClassName = useCallback((index) => {
    const classes = ['transcript-segment'];
    if (index === activeSegmentIndex) {
      classes.push('transcript-segment--active');
    }
    return classes.join(' ');
  }, [activeSegmentIndex]);

  /**
   * registerSegmentRef — Call this with a ref callback on each segment element
   * to enable auto-scrolling.
   * Usage: <div ref={(el) => registerSegmentRef(index, el)} ...>
   */
  const registerSegmentRef = useCallback((index, element) => {
    if (element) {
      segmentRefs.current.set(index, element);
    } else {
      segmentRefs.current.delete(index);
    }
  }, []);

  // Compute active segment ID
  const activeSegmentId = activeSegmentIndex >= 0 && segments[activeSegmentIndex]
    ? (segments[activeSegmentIndex].segmentId || segments[activeSegmentIndex].SegmentId || null)
    : null;

  // Reset state when segments change
  useEffect(() => {
    setActiveSegmentIndex(-1);
    lastScrolledIndex.current = -1;
  }, [segments]);

  return {
    // State
    activeSegmentIndex,
    activeSegmentId,
    isPlaying,
    currentTime,

    // Actions
    seekToSegment,
    seekToTime,
    handleTimeUpdate,

    // Helpers
    getSegmentClassName,
    registerSegmentRef,

    // Utilities
    videoTimeToTranscriptTime,
    transcriptTimeToVideoTime,
  };
};

export default useVideoSync;
