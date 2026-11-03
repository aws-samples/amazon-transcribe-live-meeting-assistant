/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting page's one player: the meeting's video recording when it has
 * one, and otherwise its audio recording, above the transcript and kept in
 * step with it (see playback-sync.js for the arithmetic, and
 * docs/web-ui-guide.md for the behaviour).
 *
 * Its own controls rather than the browser's, so the seek bar can carry the
 * meeting's chapters as marks: play/pause, the time, the seek bar, the speed,
 * mute and (for video) full screen. Each has an accessible name; the seek bar
 * is a native range input, so it is operable from the keyboard as it is.
 *
 * The page drives it through `controllerRef`: `seek(recordingSeconds, { play })`,
 * `toggle()`, `pause()` and `nudge(seconds)`. A request made before the
 * recording's metadata has loaded is kept and applied once it has. Where it
 * is, on the transcript's clock, is published to `store` for the transcript
 * to follow.
 *
 * The recording is a private S3 object, read through a link signed with the
 * viewer's credentials. A signed link expires, and a long recording outlives
 * it (the player keeps fetching ranges as it plays), so a failed load signs
 * the link again and carries on from the same place, at most once a minute.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, ButtonDropdown, SegmentedControl, SpaceBetween, Spinner } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import generateS3PresignedUrl from '../common/generate-s3-presigned-url';
import { IDENTITY_CLOCK, chapterAt, chapterMarkers, formatClock, toTranscriptSeconds } from './playback-sync';
import './MeetingPlayer.css';

const logger = new ConsoleLogger('MeetingPlayer');

const SPEEDS = [0.75, 1, 1.25, 1.5, 2];
const RESIGN_AFTER_MS = 60 * 1000;

const MeetingPlayer = ({
  kind,
  url,
  controllerRef = null,
  clock = IDENTITY_CLOCK,
  chapters = [],
  store = null,
  alternative = null,
  onSwitch = undefined,
  hint = null,
  sign = generateS3PresignedUrl,
}) => {
  const { currentCredentials } = useAppContext();
  const mediaRef = useRef(null);
  const frameRef = useRef(null);
  const [signedUrl, setSignedUrl] = useState(null);
  const [failed, setFailed] = useState(false);
  const [duration, setDuration] = useState(0);
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [fullScreen, setFullScreen] = useState(false);
  // A seek asked for before the recording could take it.
  const pendingRef = useRef(null);
  // Where to carry on from after the link was signed again.
  const resumeRef = useRef(null);
  const lastSignRef = useRef(0);
  const engagedRef = useRef(false);
  const seekCountRef = useRef(0);
  const credentialsRef = useRef(currentCredentials);
  credentialsRef.current = currentCredentials;
  const clockRef = useRef(clock);
  clockRef.current = clock;
  // True from a change of recording until the new one's metadata has loaded:
  // a seek asked for in between is for the new recording, not the old one.
  const loadingRef = useRef(true);

  // Signed once per recording. Not again when the session's credentials are
  // refreshed: a new link reloads the element and loses the place.
  useEffect(() => {
    let current = true;
    setSignedUrl(null);
    setFailed(false);
    setDuration(0);
    setPosition(0);
    engagedRef.current = false;
    loadingRef.current = true;
    if (store) store.reset();
    if (!url) return undefined;
    sign(url, credentialsRef.current)
      .then((signed) => {
        if (current) setSignedUrl(signed);
      })
      .catch((error) => {
        logger.error('could not sign the recording link', error);
        if (current) setFailed(true);
      });
    return () => {
      current = false;
    };
  }, [url, sign]);

  const publish = useCallback(() => {
    const media = mediaRef.current;
    if (!media || !store) return;
    store.set({
      seconds: toTranscriptSeconds(clockRef.current, media.currentTime),
      playing: !media.paused && !media.ended,
      engaged: engagedRef.current,
      seekCount: seekCountRef.current,
    });
  }, [store]);

  // The clock can be learnt after the recording has loaded (the key frames
  // load on their own); the transcript's position follows it.
  useEffect(() => {
    publish();
  }, [clock, publish]);

  const apply = useCallback(
    ({ seconds, play }) => {
      const media = mediaRef.current;
      if (!media) return;
      engagedRef.current = true;
      seekCountRef.current += 1;
      const end = Number.isFinite(media.duration) ? media.duration : Infinity;
      // eslint-disable-next-line no-param-reassign
      media.currentTime = Math.min(Math.max(0, seconds), end);
      setPosition(media.currentTime);
      if (play) media.play()?.catch((error) => logger.debug('playing was not allowed', error));
      if (play === false && !media.paused) media.pause();
      publish();
    },
    [publish],
  );

  const seek = useCallback(
    (seconds, { play } = {}) => {
      if (!Number.isFinite(Number(seconds))) return;
      const request = { seconds: Number(seconds), play };
      const media = mediaRef.current;
      if (!media || loadingRef.current || media.readyState < 1) {
        pendingRef.current = request;
        return;
      }
      apply(request);
    },
    [apply],
  );

  const toggle = useCallback(() => {
    const media = mediaRef.current;
    if (!media) return;
    engagedRef.current = true;
    if (media.paused || media.ended) {
      media.play()?.catch((error) => logger.debug('playing was not allowed', error));
    } else {
      media.pause();
    }
  }, []);

  const pause = useCallback(() => {
    const media = mediaRef.current;
    if (media && !media.paused) media.pause();
  }, []);

  const nudge = useCallback(
    (delta) => {
      const media = mediaRef.current;
      if (!media) return;
      seek(media.currentTime + delta, {});
    },
    [seek],
  );

  useImperativeHandle(controllerRef, () => ({ seek, toggle, pause, nudge }), [seek, toggle, pause, nudge]);

  const onLoadedMetadata = () => {
    const media = mediaRef.current;
    if (!media) return;
    loadingRef.current = false;
    setDuration(Number.isFinite(media.duration) ? media.duration : 0);
    media.playbackRate = speed;
    const resume = resumeRef.current;
    resumeRef.current = null;
    const pending = pendingRef.current || resume;
    pendingRef.current = null;
    if (pending) apply(pending);
  };

  const onError = () => {
    const media = mediaRef.current;
    if (!url || Date.now() - lastSignRef.current < RESIGN_AFTER_MS) {
      setFailed(true);
      return;
    }
    lastSignRef.current = Date.now();
    resumeRef.current = media ? { seconds: media.currentTime, play: playing } : null;
    loadingRef.current = true;
    sign(url, credentialsRef.current)
      .then(setSignedUrl)
      .catch(() => setFailed(true));
  };

  const onTimeUpdate = () => {
    const media = mediaRef.current;
    if (!media) return;
    setPosition(media.currentTime);
    publish();
  };

  const onPlayState = () => {
    const media = mediaRef.current;
    setPlaying(Boolean(media && !media.paused && !media.ended));
    publish();
  };

  // Sticky below the application's top navigation, which is sticky itself;
  // measured as it changes (its height settles once its fonts have loaded).
  useEffect(() => {
    const nav = document.getElementById('top-navigation');
    const frame = frameRef.current;
    if (!nav || !frame) return undefined;
    const place = () => frame.style.setProperty('--meeting-player-top', `${nav.offsetHeight}px`);
    place();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(place);
    observer.observe(nav);
    return () => observer.disconnect();
  }, [signedUrl, failed]);

  useEffect(() => {
    const onChange = () => setFullScreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const toggleFullScreen = () => {
    if (document.fullscreenElement) {
      document.exitFullscreen?.();
    } else {
      frameRef.current?.requestFullscreen?.()?.catch((error) => logger.debug('full screen refused', error));
    }
  };

  const changeSpeed = (rate) => {
    setSpeed(rate);
    if (mediaRef.current) mediaRef.current.playbackRate = rate;
  };

  const toggleMute = () => {
    const next = !muted;
    setMuted(next);
    if (mediaRef.current) mediaRef.current.muted = next;
  };

  const markers = useMemo(() => chapterMarkers(chapters, duration, clock), [chapters, duration, clock]);
  const chapter = chapterAt(markers, position);
  const ready = duration > 0;
  const progress = ready ? Math.min(100, (position / duration) * 100) : 0;
  const label = kind === 'video' ? 'Meeting video recording' : 'Meeting audio recording';

  if (failed) {
    return (
      <div className="meeting-player" data-testid="meeting-player">
        <Box color="text-status-error">The recording could not be loaded.</Box>
      </div>
    );
  }

  const mediaProps = {
    ref: mediaRef,
    src: signedUrl || undefined,
    preload: 'metadata',
    onLoadedMetadata,
    onDurationChange: () => {
      const media = mediaRef.current;
      if (media && Number.isFinite(media.duration)) setDuration(media.duration);
    },
    onTimeUpdate,
    onSeeked: onTimeUpdate,
    onPlay: onPlayState,
    onPause: onPlayState,
    onEnded: onPlayState,
    onError,
    'aria-label': label,
  };

  return (
    <section
      className={`meeting-player meeting-player-kind-${kind}`}
      ref={frameRef}
      aria-label={label}
      data-testid="meeting-player"
    >
      {/* The meeting's own words are its captions: the transcript beneath. */}
      {kind === 'video'
        ? React.createElement('video', {
            ...mediaProps,
            className: 'meeting-player-video',
            onClick: toggle,
            playsInline: true,
          })
        : React.createElement('audio', mediaProps)}
      <div className="meeting-player-controls">
        <Button
          variant="icon"
          iconName={playing ? 'pause' : 'play'}
          ariaLabel={playing ? 'Pause' : 'Play'}
          disabled={!signedUrl}
          onClick={toggle}
        />
        <span className="meeting-player-time" data-testid="meeting-player-time">
          {`${formatClock(position)} / ${ready ? formatClock(duration) : '--:--'}`}
        </span>
        <div className="meeting-player-seek">
          <div className="meeting-player-track" aria-hidden="true">
            <div className="meeting-player-progress" style={{ width: `${progress}%` }} />
          </div>
          <input
            type="range"
            className="meeting-player-range"
            min={0}
            max={ready ? duration : 0}
            step={0.1}
            value={Math.min(position, duration || 0)}
            disabled={!ready}
            aria-label="Seek"
            aria-valuetext={`${formatClock(position)} of ${formatClock(duration)}${
              chapter ? `, chapter ${chapter.title}` : ''
            }`}
            onChange={(event) => seek(Number(event.target.value), {})}
          />
          {markers.map((marker) => (
            <button
              type="button"
              key={marker.key}
              className="meeting-player-marker"
              style={{ left: `${marker.percent}%` }}
              aria-label={`Chapter at ${marker.label}: ${marker.title}`}
              data-testid="meeting-player-marker"
              onClick={() => seek(marker.recordingSeconds, { play: true })}
            >
              <span className="meeting-player-marker-tip" aria-hidden="true">
                <span className="meeting-player-marker-time">{marker.label}</span> {marker.title}
              </span>
            </button>
          ))}
        </div>
        <ButtonDropdown
          variant="normal"
          ariaLabel={`Playback speed, ${speed}x`}
          expandToViewport
          items={SPEEDS.map((rate) => ({ id: String(rate), text: `${rate}x`, checked: rate === speed }))}
          onItemClick={({ detail }) => changeSpeed(Number(detail.id))}
        >
          {`${speed}x`}
        </ButtonDropdown>
        <Button
          variant="icon"
          iconName={muted ? 'audio-off' : 'audio-full'}
          ariaLabel={muted ? 'Unmute' : 'Mute'}
          onClick={toggleMute}
        />
        {kind === 'video' && (
          <Button
            variant="icon"
            iconName={fullScreen ? 'exit-full-screen' : 'full-screen'}
            ariaLabel={fullScreen ? 'Exit full screen' : 'Full screen'}
            onClick={toggleFullScreen}
          />
        )}
      </div>
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        {alternative && onSwitch && (
          <SegmentedControl
            label="Recording to play"
            selectedId={kind}
            onChange={({ detail }) => onSwitch(detail.selectedId)}
            options={[
              { id: 'video', text: 'Video' },
              { id: 'audio', text: 'Audio only' },
            ]}
          />
        )}
        {!signedUrl && <Spinner />}
        {chapter && (
          <Box variant="small" color="text-body-secondary">
            <span data-testid="meeting-player-chapter">{`Chapter: ${chapter.title}`}</span>
          </Box>
        )}
      </SpaceBetween>
      {hint && (
        <Box variant="small" color="text-body-secondary">
          <span data-testid="meeting-player-hint">{hint}</span>
        </Box>
      )}
    </section>
  );
};

MeetingPlayer.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  controllerRef: PropTypes.object,
  kind: PropTypes.oneOf(['audio', 'video']).isRequired,
  url: PropTypes.string.isRequired,
  clock: PropTypes.shape({
    anchors: PropTypes.arrayOf(PropTypes.shape({ transcript: PropTypes.number, recording: PropTypes.number })),
  }),
  chapters: PropTypes.arrayOf(
    PropTypes.shape({ seconds: PropTypes.number, label: PropTypes.string, title: PropTypes.string }),
  ),
  // eslint-disable-next-line react/forbid-prop-types
  store: PropTypes.object,
  alternative: PropTypes.oneOf(['audio', 'video', null]),
  // Why this recording is the one playing, when that is not the obvious one.
  hint: PropTypes.string,
  onSwitch: PropTypes.func,
  sign: PropTypes.func,
};

export default MeetingPlayer;
