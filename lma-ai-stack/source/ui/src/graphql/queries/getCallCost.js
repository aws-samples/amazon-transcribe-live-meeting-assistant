/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The itemised cost estimate of one meeting, computed by the backend when read
 * from the meeting's metering records and the price table in force. See
 * docs/meeting-cost-estimate.md.
 */
export default /* GraphQL */ `
  query GetCallCost($CallId: ID!) {
    getCallCost(CallId: $CallId) {
      CallId
      Currency
      TotalCost
      ContextTotals {
        Context
        Cost
      }
      Items {
        Context
        Service
        Region
        Unit
        Quantity
        UnitPrice
        Cost
        Chargeable
        Priced
        Note
        Details
      }
      UnpricedCount
      RecordCount
      PricesAsOf
      PriceSource
      Region
      BaseRegion
      RegionsWithoutListedPrices
      NotIncluded
      ComputedAt
    }
  }
`;
