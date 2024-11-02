// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import gql from 'graphql-tag';

export default gql`
  mutation Mutation($input: MeetingControlsInput!) {
    meetingControls(input: $input) {
      CallId
      PK
      SK
    }
  }
`;
