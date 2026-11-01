/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useEffect, useRef, useState } from 'react';

import useAppContext from '../../contexts/app';
import generateS3PresignedUrl from '../common/generate-s3-presigned-url';
import './VideoPlayer.css';

const logger = new ConsoleLogger('VideoPlayer');

/*
 * VideoPlayer — plays the VP screen recording (.mp4) for a call.
 *
 * Mirrors RecordingPlayer (audio): the recording's S3 URL is stored on the
 * Call record (Call.VideoRecordingUrl, written by the call_event_processor
 * from the VP's ADD_S3_VIDEO_RECORDING_URL Kinesis event). We presign that
 * exact URL with the user's Cognito credentials and hand it to a native
 * <video> element. No S3 listing or filename guessing.
 *
 * `seekRequest` ({ seconds, at }) plays the recording from a time -- the meeting
 * page's key frame strip sends one when a frame is selected. `at` makes two
 * requests for the same time distinct.
 */
/* eslint-disable react/prop-types, react/destructuring-assignment */
export const VideoPlayer = ({ videoRecordingUrl, seekRequest }) => {
  const [preSignedUrl, setPreSignedUrl] = useState();
  const { setErrorMessage, currentCredentials } = useAppContext();
  const videoRef = useRef(null);
  // The request last applied. The URL is signed again whenever the session's
  // credentials are refreshed, and a request must not be applied again then:
  // it would jump back to that time however far the viewer has moved since.
  const appliedRef = useRef(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !seekRequest || !Number.isFinite(seekRequest.seconds)) return undefined;
    if (appliedRef.current === seekRequest.at) return undefined;
    const play = () => {
      appliedRef.current = seekRequest.at;
      video.currentTime = Math.max(0, seekRequest.seconds);
      video.play()?.catch((error) => logger.debug('video play was not allowed', error));
    };
    if (video.scrollIntoView) video.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    // Before its metadata has loaded a video cannot be positioned.
    if (video.readyState >= 1) {
      play();
      return undefined;
    }
    video.addEventListener('loadedmetadata', play, { once: true });
    return () => video.removeEventListener('loadedmetadata', play);
  }, [seekRequest, preSignedUrl]);

  useEffect(() => {
    const fetchUrl = async () => {
      if (videoRecordingUrl) {
        logger.debug('video recording url to presign', videoRecordingUrl);
        try {
          const url = await generateS3PresignedUrl(videoRecordingUrl, currentCredentials);
          logger.debug('video recording presigned url', url);
          setPreSignedUrl(url);
        } catch (error) {
          setErrorMessage('failed to get video recording url - please try again later');
          logger.error('failed generate video recording s3 url', error);
        }
      }
    };
    fetchUrl();
  }, [videoRecordingUrl, currentCredentials]);

  return preSignedUrl?.length ? (
    <video ref={videoRef} className="video-player" src={preSignedUrl} controls preload="metadata">
      <track kind="captions" />
    </video>
  ) : null;
};

export default VideoPlayer;
