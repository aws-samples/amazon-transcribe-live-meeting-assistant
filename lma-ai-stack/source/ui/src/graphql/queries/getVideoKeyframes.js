/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
  query GetVideoKeyframes($callId: ID!, $limit: Int, $nextToken: String, $afterFrameId: ID, $frameId: ID) {
    getVideoKeyframes(
      callId: $callId
      limit: $limit
      nextToken: $nextToken
      afterFrameId: $afterFrameId
      frameId: $frameId
    ) {
      VideoKeyframes {
        CallId
        FrameId
        Timestamp
        VideoOffset
        Width
        Height
        Source
        ShareDetected
        SlideText
        ImageSignedUrl
        ThumbnailSignedUrl
      }
      nextToken
    }
  }
`;
