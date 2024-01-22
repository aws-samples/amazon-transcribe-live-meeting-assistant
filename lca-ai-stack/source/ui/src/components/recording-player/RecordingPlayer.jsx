// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState } from 'react';
import { Logger } from 'aws-amplify';
import ReactAudioPlayer from 'react-audio-player';

import useAppContext from '../../contexts/app';
import generateS3PresignedUrl from '../common/generate-s3-presigned-url';

const logger = new Logger('RecordingPlayer');

/* eslint-disable react/prop-types, react/destructuring-assignment */
export const RecordingPlayer = ({ recordingUrl }) => {
  const [preSignedUrl, setPreSignedUrl] = useState();
  const { setErrorMessage, currentCredentials } = useAppContext();

  useEffect(async () => {
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
  }, [recordingUrl, currentCredentials]);

  return preSignedUrl?.length ? <ReactAudioPlayer src={preSignedUrl} controls /> : null;
};

export default RecordingPlayer;
