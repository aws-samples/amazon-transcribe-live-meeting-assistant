/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
  query GetMeetingArchive($CallId: ID!, $ArchiveId: ID!) {
    getMeetingArchive(CallId: $CallId, ArchiveId: $ArchiveId) {
      CallId
      ArchiveId
      Contents
      Status
      FileName
      Size
      DownloadUrl
      Message
      CreatedAt
      UpdatedAt
    }
  }
`;
