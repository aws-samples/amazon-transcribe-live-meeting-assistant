/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
  query GetChatAttachments($callId: ID!, $attachmentId: ID, $nextToken: String) {
    getChatAttachments(callId: $callId, attachmentId: $attachmentId, nextToken: $nextToken) {
      ChatAttachments {
        CallId
        AttachmentId
        SegmentId
        Timestamp
        Kind
        FileName
        Status
        NotStoredReason
        ContentType
        Size
        Summarize
        Title
        Summary
        SummaryState
        DownloadUrl
        PreviewUrl
      }
      nextToken
    }
  }
`;
