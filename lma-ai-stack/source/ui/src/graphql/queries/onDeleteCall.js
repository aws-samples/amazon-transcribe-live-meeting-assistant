/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import gql from 'graphql-tag';

export default gql`
  subscription Subscription {
    onDeleteCall {
      CallId
      Owner
      SharedWith
    }
  }
`;
