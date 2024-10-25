// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import gql from 'graphql-tag';

export default gql`
  query Query($date: AWSDate, $shard: Int) {
    listCallsDateShard(date: $date, shard: $shard) {
      Calls {
        CallId
        PK
        SK
      }
      nextToken
    }
  }
`;
