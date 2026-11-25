/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The origins an embed accepts messages from: the `allowedOrigins` query
 * parameter's, or the embed's own origin when the parameter is empty. Outgoing
 * events use the same rule (they go to the first of these origins).
 *
 * @param {string[]} allowedOrigins - The embed's `allowedOrigins` query parameter, split
 * @returns {string[]}
 */
export const embedMessageOrigins = (allowedOrigins = []) =>
  allowedOrigins.length > 0 ? allowedOrigins : [window.location.origin];

/**
 * Whether a `message` event comes from an origin the embed accepts.
 *
 * @param {MessageEvent} event
 * @param {string[]} allowedOrigins - The embed's `allowedOrigins` query parameter, split
 * @returns {boolean}
 */
export const isAcceptedEmbedMessage = (event, allowedOrigins = []) =>
  embedMessageOrigins(allowedOrigins).includes(event.origin);
