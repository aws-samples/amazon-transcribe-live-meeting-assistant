/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useEffect, useState } from 'react';
import ReactAudioPlayer from 'react-audio-player';

import useAppContext from '../../contexts/app';
import generateS3PresignedUrl from '../common/generate-s3-presigned-url';

const logger = new ConsoleLogger('RecordingPlayer');

/* eslint-disable react/prop-types, react/destructuring-assignment */
export const RecordingPlayer = ({ recordingUrl }) => {
  const [preSignedUrl, setPreSignedUrl] = useState();
  const { setErrorMessage, currentCredentials } = useAppContext();

  useEffect(() => {
    const fetchUrl = async () => {
      if (recordingUrl) {
        let url;
        logger.debug('recording url to presign', recordingUrl);
        try {
          url = await generateS3PresignedUrl(recordingUrl, currentCredentials);
          logger.debug('recording presigned url', url);
          setPreSignedUrl(url);
        } catch (error) {
          setErrorMessage('failed to get recording url - please try again later');
          logger.error('failed generate recording s3 url', error);
        }
      }
    };
    fetchUrl();
  }, [recordingUrl, currentCredentials]);

  return preSignedUrl?.length ? <ReactAudioPlayer src={preSignedUrl} controls /> : null;
};

export default RecordingPlayer;
