/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { useState, useEffect, useCallback, useRef } from 'react';

const logger = new ConsoleLogger('usePostMessageAuth');

/**
 * Hook that listens for authentication tokens via the postMessage Web API.
 * This enables parent applications to pass Cognito tokens to the LMA embed iframe.
 *
 * Message protocol:
 * - Parent → iframe: { type: 'LMA_AUTH', idToken, accessToken, refreshToken }
 * - Parent → iframe: { type: 'LMA_AUTH_REFRESH', idToken, accessToken, refreshToken }
 * - iframe → Parent: { type: 'LMA_AUTH_SUCCESS' }
 * - iframe → Parent: { type: 'LMA_AUTH_ERROR', error: '...' }
 *
 * @param {Object} options
 * @param {boolean} options.enabled - Whether to listen for postMessage auth
 * @param {string[]} options.allowedOrigins - List of allowed parent origins (empty = allow all)
 * @returns {{ isAuthenticated, isWaiting, error, tokens }}
 */
const usePostMessageAuth = ({ enabled = false, allowedOrigins = [] } = {}) => {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isWaiting, setIsWaiting] = useState(enabled);
  const [error, setError] = useState(null);
  const [tokens, setTokens] = useState(null);
  const tokensRef = useRef(null);

  const sendToParent = useCallback((message) => {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(message, '*');
      }
      if (window.opener) {
        window.opener.postMessage(message, '*');
      }
    } catch (err) {
      logger.warn('Failed to send message to parent:', err);
    }
  }, []);

  const handleTokens = useCallback(
    async (tokenData) => {
      try {
        logger.info('Received auth tokens via postMessage');

        const { idToken, accessToken, refreshToken } = tokenData;

        if (!idToken || !accessToken) {
          throw new Error('Missing required tokens (idToken and accessToken are required)');
        }

        // Store tokens for use by components
        const tokenObj = { idToken, accessToken, refreshToken };
        tokensRef.current = tokenObj;
        setTokens(tokenObj);
        setIsAuthenticated(true);
        setIsWaiting(false);
        setError(null);

        sendToParent({ type: 'LMA_AUTH_SUCCESS' });
        logger.info('Authentication via postMessage successful');
      } catch (err) {
        logger.error('Failed to process auth tokens:', err);
        setError(err.message || 'Authentication failed');
        setIsWaiting(false);
        sendToParent({ type: 'LMA_AUTH_ERROR', error: err.message });
      }
    },
    [sendToParent],
  );

  useEffect(() => {
    if (!enabled) return undefined;

    const handleMessage = (event) => {
      // Validate origin if allowedOrigins is specified
      if (allowedOrigins.length > 0 && !allowedOrigins.includes(event.origin)) {
        logger.warn(`Rejected postMessage from unauthorized origin: ${event.origin}`);
        return;
      }

      const { data } = event;
      if (!data || !data.type) return;

      switch (data.type) {
        case 'LMA_AUTH':
          handleTokens(data);
          break;

        case 'LMA_AUTH_REFRESH':
          logger.info('Received token refresh via postMessage');
          handleTokens(data);
          break;

        default:
          // Ignore unrelated messages
          break;
      }
    };

    window.addEventListener('message', handleMessage);
    logger.info('PostMessage auth listener active, waiting for tokens...');

    // Notify parent that we're ready to receive tokens
    sendToParent({ type: 'LMA_AUTH_READY' });

    return () => {
      window.removeEventListener('message', handleMessage);
    };
  }, [enabled, allowedOrigins, handleTokens, sendToParent]);

  return {
    isAuthenticated,
    isWaiting,
    error,
    tokens,
    sendToParent,
  };
};

export default usePostMessageAuth;
