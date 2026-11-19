/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

export default /* GraphQL */ `
  mutation PrepareMeetingArchive($CallId: ID!, $Contents: MeetingArchiveContents) {
    prepareMeetingArchive(CallId: $CallId, Contents: $Contents) {
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
