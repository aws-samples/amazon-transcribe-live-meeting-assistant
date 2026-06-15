/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useEffect, useState } from 'react';

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
 */
/* eslint-disable react/prop-types, react/destructuring-assignment */
export const VideoPlayer = ({ videoRecordingUrl }) => {
  const [preSignedUrl, setPreSignedUrl] = useState();
  const { setErrorMessage, currentCredentials } = useAppContext();

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
    <video className="video-player" src={preSignedUrl} controls preload="metadata">
      <track kind="captions" />
    </video>
  ) : null;
};

export default VideoPlayer;
