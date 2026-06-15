/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { S3Client, ListObjectsV2Command, HeadObjectCommand } from '@aws-sdk/client-s3';
import generateS3PresignedUrl from '../common/generate-s3-presigned-url';

const logger = new ConsoleLogger('VideoAPI');

// Configuration — these should come from environment/settings
const VIDEO_BUCKET = process.env.REACT_APP_VIDEO_BUCKET || '';
const VIDEO_PREFIX = 'lma-video-recordings/';
const VIDEO_REGION = process.env.REACT_APP_AWS_REGION || 'us-east-1';

/**
 * Get the video recording URL for a given callId.
 *
 * Video recordings are stored in S3 at:
 *   s3://{bucket}/lma-video-recordings/call_{callId}_{timestamp}.mp4
 *
 * Strategy:
 *   1. List objects with prefix `lma-video-recordings/call_{callId}_`
 *   2. Return the most recent one (by LastModified)
 *   3. If none found, return { status: 'not-found' }
 *   4. If object exists but is 0 bytes or very small, return { status: 'processing' }
 *
 * @param {string} callId — The call/meeting ID
 * @param {Object} credentials — AWS credentials from Cognito
 * @returns {Promise<{ status: string, url?: string, key?: string }>}
 */
export const getVideoRecordingUrl = async (callId, credentials) => {
  if (!callId) {
    return { status: 'not-found' };
  }

  if (!VIDEO_BUCKET) {
    logger.warn('VIDEO_BUCKET not configured — video playback disabled');
    return { status: 'not-found' };
  }

  try {
    const s3Client = new S3Client({
      region: VIDEO_REGION,
      credentials,
    });

    // List objects matching this callId
    const prefix = `${VIDEO_PREFIX}call_${callId}_`;
    logger.debug('Listing video objects with prefix:', prefix);

    const listCommand = new ListObjectsV2Command({
      Bucket: VIDEO_BUCKET,
      Prefix: prefix,
      MaxKeys: 10,
    });

    const listResponse = await s3Client.send(listCommand);

    if (!listResponse.Contents || listResponse.Contents.length === 0) {
      logger.debug('No video recordings found for callId:', callId);
      return { status: 'not-found' };
    }

    // Sort by LastModified descending — get the most recent recording
    const sortedObjects = listResponse.Contents.sort(
      (a, b) => new Date(b.LastModified) - new Date(a.LastModified)
    );

    const latestObject = sortedObjects[0];
    logger.debug('Found video recording:', latestObject.Key, 'Size:', latestObject.Size);

    // If the file is very small, it might still be processing/uploading
    if (latestObject.Size < 1000) {
      logger.debug('Video file too small, likely still processing');
      return { status: 'processing', key: latestObject.Key };
    }

    // Construct the S3 URL
    const s3Url = `https://${VIDEO_BUCKET}.s3.${VIDEO_REGION}.amazonaws.com/${latestObject.Key}`;

    return {
      status: 'ready',
      url: s3Url,
      key: latestObject.Key,
      size: latestObject.Size,
      lastModified: latestObject.LastModified,
    };
  } catch (error) {
    logger.error('Error looking up video recording:', error);

    // If access denied, the bucket might not have video recording enabled
    if (error.name === 'AccessDenied' || error.$metadata?.httpStatusCode === 403) {
      return { status: 'not-found' };
    }

    throw error;
  }
};

/**
 * List all video recordings available in the bucket.
 * Useful for admin/debugging views.
 *
 * @param {Object} credentials — AWS credentials from Cognito
 * @param {Object} [options]
 * @param {number} [options.maxResults=50] — Maximum results to return
 * @param {string} [options.continuationToken] — For pagination
 * @returns {Promise<{ recordings: Array, nextToken?: string }>}
 */
export const listVideoRecordings = async (credentials, options = {}) => {
  const { maxResults = 50, continuationToken } = options;

  if (!VIDEO_BUCKET) {
    logger.warn('VIDEO_BUCKET not configured');
    return { recordings: [] };
  }

  try {
    const s3Client = new S3Client({
      region: VIDEO_REGION,
      credentials,
    });

    const command = new ListObjectsV2Command({
      Bucket: VIDEO_BUCKET,
      Prefix: VIDEO_PREFIX,
      MaxKeys: maxResults,
      ContinuationToken: continuationToken,
    });

    const response = await s3Client.send(command);

    const recordings = (response.Contents || [])
      .filter((obj) => obj.Key.endsWith('.mp4') && obj.Size > 1000)
      .map((obj) => {
        // Extract callId from key: lma-video-recordings/call_{callId}_{timestamp}.mp4
        const filename = obj.Key.replace(VIDEO_PREFIX, '');
        const match = filename.match(/^call_(.+?)_(\d+)\.mp4$/);

        return {
          key: obj.Key,
          callId: match ? match[1] : null,
          timestamp: match ? new Date(parseInt(match[2], 10)) : null,
          size: obj.Size,
          lastModified: obj.LastModified,
          url: `https://${VIDEO_BUCKET}.s3.${VIDEO_REGION}.amazonaws.com/${obj.Key}`,
        };
      })
      .filter((r) => r.callId); // Only include valid recordings

    return {
      recordings,
      nextToken: response.IsTruncated ? response.NextContinuationToken : undefined,
    };
  } catch (error) {
    logger.error('Error listing video recordings:', error);
    return { recordings: [] };
  }
};

/**
 * Check if a video recording exists for a callId without fetching the full URL.
 * Lighter weight than getVideoRecordingUrl — good for conditional rendering.
 *
 * @param {string} callId
 * @param {Object} credentials
 * @returns {Promise<boolean>}
 */
export const hasVideoRecording = async (callId, credentials) => {
  const result = await getVideoRecordingUrl(callId, credentials);
  return result.status === 'ready';
};

export default {
  getVideoRecordingUrl,
  listVideoRecordings,
  hasVideoRecording,
};
