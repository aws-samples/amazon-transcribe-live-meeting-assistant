/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedPage - A chrome-free component page designed for iframe embedding.
 * Renders individual LMA components controlled via URL query parameters.
 *
 * Usage: /#/embed?component=<name>&param1=value1&param2=value2
 *
 * Query Parameters:
 *   component    - Component to render: stream-audio, transcript, summary, chat,
 *                  vnc, vp-details, call-details, meeting-loader
 *   show         - Comma-separated panels to display: transcript,summary,chat,vnc
 *   layout       - Layout arrangement: horizontal, vertical, grid (default: vertical)
 *   callId       - Load existing meeting by ID
 *   vpId         - Load virtual participant by ID
 *   meetingTopic - Pre-fill meeting topic
 *   participants - Pre-fill participant label
 *   owner        - Pre-fill meeting owner
 *   autoStart    - Auto-start streaming (true/false)
 *   authMode     - Authentication mode: cognito (default), token
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useMemo, useEffect, useCallback } from 'react';
import PropTypes from 'prop-types';
import { useLocation } from 'react-router-dom';
import { Box, Spinner } from '@cloudscape-design/components';

import usePostMessageAuth from '../../hooks/use-postmessage-auth';
import useAppContext from '../../contexts/app';
import ComponentSelector from './ComponentSelector';

import './EmbedPage.css';

const logger = new ConsoleLogger('EmbedPage');

/**
 * Parse query parameters from the URL hash.
 * Since we use HashRouter, params are after the hash path.
 */
const useEmbedParams = () => {
  const location = useLocation();

  return useMemo(() => {
    const searchParams = new URLSearchParams(location.search);

    const params = {
      // Component selection
      component: searchParams.get('component') || 'stream-audio',
      show: searchParams.get('show')
        ? searchParams
            .get('show')
            .split(',')
            .map((s) => s.trim())
        : [],
      layout: searchParams.get('layout') || 'vertical',

      // Meeting parameters
      callId: searchParams.get('callId') || '',
      vpId: searchParams.get('vpId') || '',
      meetingTopic: searchParams.get('meetingTopic') || '',
      participants: searchParams.get('participants') || '',
      owner: searchParams.get('owner') || '',
      autoStart: searchParams.get('autoStart') === 'true',

      // Authentication
      authMode: searchParams.get('authMode') || 'cognito',

      allowedOrigins: searchParams.get('allowedOrigins')
        ? searchParams
            .get('allowedOrigins')
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean)
        : [],
    };

    logger.debug('Embed params:', params);
    return params;
  }, [location.search]);
};

/**
 * Sends events to the parent window via postMessage.
 * Uses a specific target origin (from allowedOrigins) rather than '*' to prevent
 * delivering messages to a malicious origin if the parent frame is ever navigated.
 */
const useParentMessaging = (allowedOrigins = []) => {
  const sendToParent = useCallback(
    (message) => {
      try {
        const targetOrigin = allowedOrigins.length > 0 ? allowedOrigins[0] : window.location.origin;
        if (window.parent && window.parent !== window) {
          window.parent.postMessage(message, targetOrigin);
        }
        if (window.opener) {
          window.opener.postMessage(message, targetOrigin);
        }
      } catch (err) {
        logger.warn('Failed to send message to parent:', err);
      }
    },
    [allowedOrigins],
  );

  return { sendToParent };
};

/**
 * Loading state shown while waiting for authentication.
 */
const AuthWaitingState = () => (
  <div className="embed-auth-waiting">
    <Box textAlign="center" padding="xxl">
      <Spinner size="large" />
      <Box margin={{ top: 'm' }} fontSize="heading-m">
        Waiting for authentication...
      </Box>
      <Box margin={{ top: 's' }} color="text-body-secondary">
        The parent application needs to send authentication tokens via postMessage.
      </Box>
      <Box margin={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
        Send: {`{ type: 'LMA_AUTH', idToken: '...', accessToken: '...', refreshToken: '...' }`}
      </Box>
    </Box>
  </div>
);

/**
 * Error state shown when authentication fails.
 */
const AuthErrorState = ({ error }) => (
  <div className="embed-auth-error">
    <Box textAlign="center" padding="xxl">
      <Box fontSize="heading-m" color="text-status-error">
        Authentication Error
      </Box>
      <Box margin={{ top: 's' }} color="text-body-secondary">
        {error}
      </Box>
    </Box>
  </div>
);

AuthErrorState.propTypes = {
  error: PropTypes.string,
};

AuthErrorState.defaultProps = {
  error: 'Unknown authentication error',
};

/**
 * Main EmbedPage component.
 * Handles authentication mode selection and renders the appropriate component.
 */
const EmbedPage = () => {
  const params = useEmbedParams();
  const { sendToParent } = useParentMessaging(params.allowedOrigins);
  const { currentCredentials, currentSession, user } = useAppContext();

  // PostMessage auth for token mode
  const {
    isAuthenticated: postMessageAuthed,
    isWaiting: postMessageWaiting,
    error: postMessageError,
  } = usePostMessageAuth({
    enabled: params.authMode === 'token',
    allowedOrigins: params.allowedOrigins,
  });

  // Listen for control messages from parent
  useEffect(() => {
    const handleMessage = (event) => {
      const { data } = event;
      if (!data || !data.type) return;

      switch (data.type) {
        case 'LMA_START_MEETING':
          logger.info('Received LMA_START_MEETING from parent');
          // This will be handled by the individual components
          break;
        case 'LMA_STOP_MEETING':
          logger.info('Received LMA_STOP_MEETING from parent');
          break;
        default:
          break;
      }
    };

    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, []);

  // Notify parent that embed page is loaded
  useEffect(() => {
    sendToParent({
      type: 'LMA_EMBED_LOADED',
      component: params.component,
      params,
    });
  }, [params.component]);

  // For token auth mode, show waiting/error states
  if (params.authMode === 'token') {
    if (postMessageWaiting) {
      return <AuthWaitingState />;
    }
    if (postMessageError) {
      return <AuthErrorState error={postMessageError} />;
    }
  }

  // For cognito auth mode, check if we have credentials (handled by AuthRoutes wrapper)
  const isAuthenticated =
    params.authMode === 'cognito' ? !!(currentCredentials && currentSession && user) : postMessageAuthed;

  if (!isAuthenticated && params.authMode === 'cognito') {
    return (
      <div className="embed-auth-waiting">
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
          <Box margin={{ top: 'm' }} fontSize="heading-m">
            Authenticating...
          </Box>
        </Box>
      </div>
    );
  }

  return (
    <div className="embed-page">
      <ComponentSelector params={params} sendToParent={sendToParent} />
    </div>
  );
};

export default EmbedPage;
