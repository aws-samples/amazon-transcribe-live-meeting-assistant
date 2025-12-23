/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useState } from 'react';
import { Amplify, API } from 'aws-amplify';
import { Box, SpaceBetween, Spinner } from '@awsui/components-react';
import { handleOAuthCallback } from '../../graphql/mutations';
import awsExports from '../../aws-exports';

/**
 * OAuth Callback Handler Component
 * Handles the OAuth authorization callback and exchanges code for tokens
 */
const OAuthCallback = () => {
  const [status, setStatus] = useState('processing');
  const [message, setMessage] = useState('Completing authorization...');

  useEffect(() => {
    // Configure Amplify in popup window context
    Amplify.configure(awsExports);

    const processCallback = async () => {
      try {
        // Parse URL parameters
        const params = new URLSearchParams(window.location.search);
        const code = params.get('code');
        const state = params.get('state');
        const error = params.get('error');
        const errorDescription = params.get('error_description');

        // Check for OAuth error
        if (error) {
          throw new Error(`Authorization failed: ${error}${errorDescription ? ` - ${errorDescription}` : ''}`);
        }

        if (!code || !state) {
          throw new Error('Missing authorization code or state parameter');
        }

        // Verify state matches what we stored
        const storedState = sessionStorage.getItem('oauth_state');
        if (state !== storedState) {
          throw new Error('Invalid state parameter - possible CSRF attack');
        }

        // Get code verifier and server ID from session storage
        const codeVerifier = sessionStorage.getItem('oauth_code_verifier');
        const serverId = sessionStorage.getItem('oauth_server_id');

        if (!codeVerifier || !serverId) {
          throw new Error('Missing OAuth session data');
        }

        setMessage('Exchanging authorization code for tokens...');

        // Exchange code for tokens via GraphQL
        const response = await API.graphql({
          query: handleOAuthCallback,
          variables: {
            input: {
              code,
              state,
              codeVerifier,
            },
          },
        });

        const result = response.data.handleOAuthCallback;

        if (result.success) {
          setStatus('success');
          setMessage('✅ Authorization complete! You can close this window.');

          // Notify parent window of success
          if (window.opener) {
            window.opener.postMessage(
              {
                type: 'oauth_success',
                serverId: result.serverId,
              },
              window.location.origin,
            );
          }

          // Clean up session storage
          sessionStorage.removeItem('oauth_state');
          sessionStorage.removeItem('oauth_code_verifier');
          sessionStorage.removeItem('oauth_server_id');

          // Auto-close disabled for debugging
          // setTimeout(() => {
          //   window.close();
          // }, 2000);
        } else {
          throw new Error(result.error || 'Token exchange failed');
        }
      } catch (err) {
        console.error('OAuth callback error:', err);
        console.error('Error details:', JSON.stringify(err, null, 2));
        console.error('Error message:', err.message);
        console.error('Error stack:', err.stack);
        setStatus('error');
        setMessage(`❌ Authorization failed: ${err.message || JSON.stringify(err)}`);

        // Notify parent window of error
        if (window.opener) {
          window.opener.postMessage(
            {
              type: 'oauth_error',
              error: err.message,
            },
            window.location.origin,
          );
        }

        // Auto-close disabled for debugging
        // setTimeout(() => {
        //   window.close();
        // }, 5000);
      }
    };

    processCallback();
  }, []);

  return (
    <Box padding="xxl" textAlign="center">
      <SpaceBetween size="l">
        {status === 'processing' && (
          <>
            <Spinner size="large" />
            <Box variant="h2">{message}</Box>
          </>
        )}
        {status === 'success' && (
          <>
            <Box variant="h1" color="text-status-success">
              ✅
            </Box>
            <Box variant="h2">{message}</Box>
            <Box variant="small" color="text-body-secondary">
              This window will close automatically...
            </Box>
          </>
        )}
        {status === 'error' && (
          <>
            <Box variant="h1" color="text-status-error">
              ❌
            </Box>
            <Box variant="h2">{message}</Box>
            <Box variant="small" color="text-body-secondary">
              This window will close automatically...
            </Box>
          </>
        )}
      </SpaceBetween>
    </Box>
  );
};

export default OAuthCallback;
