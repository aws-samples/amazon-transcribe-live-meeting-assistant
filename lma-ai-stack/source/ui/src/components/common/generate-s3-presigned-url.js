// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { HttpRequest } from '@aws-sdk/protocol-http';
import { S3RequestPresigner } from '@aws-sdk/s3-request-presigner';
import { parseUrl } from '@aws-sdk/url-parser';
import { Sha256 } from '@aws-crypto/sha256-browser';
import { formatUrl } from '@aws-sdk/util-format-url';
import { Logger } from 'aws-amplify';

let newUrl = '';

const generateS3PresignedUrl = async (url, credentials) => {
  const logger = new Logger('CallPanel');

  logger.debug('URL KISH:', url);
  // prettier-ignore

  const bucketName = url.split('/')[2].split('.')[0];
  const key = `${url.split('/')[3]}/${url.split('/')[4]}`;
  const region = url.split('/')[2].split('.')[2];

  newUrl = `https://${bucketName}.s3.${region}.amazonaws.com/${key}`;

  if (url.includes('detailType')) {
    newUrl = url;
  }
  logger.debug('NEW URL KISH:', newUrl);

  // const s3ObjectUrl = parseUrl(`https://${bucketName}.s3.${region}.amazonaws.com/${key}`);
  const s3ObjectUrl = parseUrl(newUrl);

  const presigner = new S3RequestPresigner({
    credentials,
    region,
    sha256: Sha256, // In browsers
  });
  // Create a GET request from S3 url.
  const presignedResponse = await presigner.presign(new HttpRequest(s3ObjectUrl));
  const presignedUrl = formatUrl(presignedResponse);
  return presignedUrl;
};

export default generateS3PresignedUrl;
