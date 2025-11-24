/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import RFB from '@novnc/novnc/lib/rfb';
import { Container, Header, SpaceBetween, Alert, Spinner, Box, Button, Toggle, Badge } from '@awsui/components-react';
import { Auth } from 'aws-amplify';

const VNCViewer = ({
  vpId,
  vncEndpoint,
  websocketUrl,
  status,
  manualActionType,
  manualActionMessage,
  manualActionTimeoutSeconds,
  manualActionStartTime,
  compact,
  onOpenNewTab,
  showHeader,
}) => {
  const canvasRef = useRef(null);
  const rfbRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(null);
  const [viewOnly, setViewOnly] = useState(true);
  const [scaleViewport, setScaleViewport] = useState(true);
  const [manualActionTimeRemaining, setManualActionTimeRemaining] = useState(0);

  // Determine if manual action is required based on props
  const manualActionRequired = status === 'MANUAL_ACTION_REQUIRED' && manualActionType;

  // Automatically disable viewOnly when manual action is required
  useEffect(() => {
    if (manualActionRequired && viewOnly === true) {
      setViewOnly(false);
    }
  }, [manualActionRequired]);

  // Calculate time remaining when manual action is required
  useEffect(() => {
    if (!manualActionRequired || !manualActionStartTime || !manualActionTimeoutSeconds) {
      setManualActionTimeRemaining(0);
      return undefined;
    }

    // Calculate initial time remaining
    const startTime = new Date(manualActionStartTime).getTime();
    const now = Date.now();
    const elapsed = Math.floor((now - startTime) / 1000);
    const remaining = Math.max(0, manualActionTimeoutSeconds - elapsed);
    setManualActionTimeRemaining(remaining);

    // Set up countdown timer
    const timer = setInterval(() => {
      setManualActionTimeRemaining((prev) => {
        if (prev <= 1) {
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(timer);
  }, [manualActionRequired, manualActionStartTime, manualActionTimeoutSeconds]);

  useEffect(() => {
    if (!canvasRef.current || !vpId || !vncEndpoint) return undefined;

    setConnecting(true);
    setError(null);

    // Get Cognito token and connect
    const connectWithAuth = async () => {
      try {
        // Get current Cognito session
        const session = await Auth.currentSession();
        const idToken = session.getIdToken().getJwtToken();

        // Append token as query parameter to the WebSocket URL
        // Format: wss://cloudfront-domain/vnc/{vpId}?token={idToken}
        const url = new URL(vncEndpoint);
        url.searchParams.append('token', idToken);
        const wsUrl = url.toString();

        console.log('Connecting to VNC via CloudFront with authentication');
        console.log('Virtual Participant ID:', vpId);

        const rfb = new RFB(canvasRef.current, wsUrl, {
          credentials: { password: '' },
        });

        // Configure RFB
        rfb.scaleViewport = scaleViewport;
        rfb.resizeSession = false;
        rfb.viewOnly = viewOnly;

        // Event handlers
        rfb.addEventListener('connect', () => {
          console.log('VNC connected successfully');
          setConnected(true);
          setConnecting(false);
          setError(null);
        });

        rfb.addEventListener('disconnect', (e) => {
          console.log('VNC disconnected:', e.detail);
          setConnected(false);
          setConnecting(false);
          if (e.detail.clean === false) {
            setError('Connection lost. The virtual participant may have ended.');
          }
        });

        rfb.addEventListener('securityfailure', (e) => {
          console.error('VNC security failure:', e.detail);
          setError(`Security failure: ${e.detail.reason}`);
          setConnecting(false);
        });

        rfb.addEventListener('credentialsrequired', () => {
          console.log('VNC credentials required');
          setError('Authentication required');
          setConnecting(false);
        });

        rfbRef.current = rfb;
      } catch (err) {
        console.error('Failed to connect:', err);
        setError(`Failed to connect: ${err.message}`);
        setConnecting(false);
      }
    };

    // Call the async function
    connectWithAuth();

    return () => {
      if (rfbRef.current) {
        rfbRef.current.disconnect();
        rfbRef.current = null;
      }
    };
  }, [vpId, vncEndpoint, websocketUrl, scaleViewport, viewOnly]);

  const handleFullscreen = () => {
    if (canvasRef.current) {
      const canvas = canvasRef.current.querySelector('canvas');
      if (canvas && canvas.requestFullscreen) {
        canvas.requestFullscreen();
      }
    }
  };

  const handleCtrlAltDel = () => {
    if (rfbRef.current) {
      rfbRef.current.sendCtrlAltDel();
    }
  };

  const handleRefresh = () => {
    if (rfbRef.current) {
      rfbRef.current.disconnect();
    }
    // Reconnection will happen automatically via useEffect
  };

  return (
    <Container
      header={
        showHeader ? (
          <Header
            variant={compact ? 'h3' : 'h2'}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Toggle checked={viewOnly} onChange={({ detail }) => setViewOnly(detail.checked)} disabled={!connected}>
                  View Only
                </Toggle>
                <Toggle
                  checked={scaleViewport}
                  onChange={({ detail }) => setScaleViewport(detail.checked)}
                  disabled={!connected}
                >
                  Scale to Fit
                </Toggle>
                <Button onClick={handleFullscreen} disabled={!connected} iconName="expand">
                  Fullscreen
                </Button>
                {compact && onOpenNewTab && (
                  <Button onClick={onOpenNewTab} iconName="external">
                    Open in New Tab
                  </Button>
                )}
                <Button onClick={handleCtrlAltDel} disabled={!connected}>
                  Ctrl+Alt+Del
                </Button>
                <Button onClick={handleRefresh} iconName="refresh">
                  Reconnect
                </Button>
              </SpaceBetween>
            }
          >
            {compact ? 'VP Live Preview' : 'Live Virtual Participant View'}
            {connected && <Badge color="green">Connected</Badge>}
            {connecting && <Badge color="blue">Connecting...</Badge>}
          </Header>
        ) : null
      }
    >
      <SpaceBetween direction="vertical" size="s">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        {connecting && !error && (
          <Box textAlign="center" padding="l">
            <Spinner size="large" />
            <Box margin={{ top: 's' }}>Connecting to virtual participant...</Box>
          </Box>
        )}

        {manualActionRequired && (
          <Alert
            type="warning"
            header={`⚠️ MANUAL ACTION REQUIRED - Time remaining: ${Math.floor(manualActionTimeRemaining / 60)}:${String(
              manualActionTimeRemaining % 60,
            ).padStart(2, '0')}`}
          >
            <SpaceBetween direction="vertical" size="xs">
              <div>
                <strong>{manualActionType}:</strong> {manualActionMessage || 'Manual action required'}
              </div>
              <div>The virtual participant will continue automatically once the action is completed.</div>
            </SpaceBetween>
          </Alert>
        )}

        {connected && !manualActionRequired && !compact && (
          <Alert type="success">
            <SpaceBetween direction="vertical" size="xs">
              <div>
                <strong>Connected</strong> - Virtual participant is active
              </div>
              <div>
                {viewOnly
                  ? 'View Only mode is enabled. Toggle it off to interact with the virtual participant.'
                  : 'Click inside the viewer to control the virtual participant with your mouse and keyboard.'}
              </div>
              {!viewOnly && (
                <div>
                  <strong>⚠️ Warning:</strong> Interacting with the virtual participant during automated steps may
                  disrupt the automation process and cause subsequent steps to fail.
                </div>
              )}
            </SpaceBetween>
          </Alert>
        )}

        <div
          style={
            scaleViewport
              ? {
                  height: compact ? '300px' : '600px',
                  overflow: 'hidden',
                }
              : {}
          }
        >
          <div
            ref={canvasRef}
            style={{
              width: '100%',
              height: (() => {
                if (scaleViewport) return '100%';
                return compact ? '300px' : '800px';
              })(),
              border: '1px solid #ccc',
              backgroundColor: '#000',
              display: 'flex',
              justifyContent: 'center',
              alignItems: 'center',
              cursor: connected ? 'default' : 'wait',
              overflow: scaleViewport ? 'hidden' : 'auto',
            }}
          />
        </div>

        {!compact && (
          <Alert type="info">
            <SpaceBetween direction="vertical" size="xs">
              <div>
                <strong>Tips for using the live view:</strong>
              </div>
              <ul style={{ margin: 0, paddingLeft: '20px' }}>
                <li>
                  <strong>View Only mode is enabled by default</strong> to prevent accidental interactions during
                  automated steps
                </li>
                <li>Only disable View Only mode when manual action is required (e.g., CAPTCHA, login prompts)</li>
                <li>
                  <strong>Warning:</strong> Interacting during automated steps may break the automation sequence
                </li>
                <li>Use your mouse and keyboard normally when handling manual actions</li>
                <li>Use &quot;Scale to Fit&quot; to adjust the display size</li>
                <li>Click &quot;Fullscreen&quot; for a larger view</li>
              </ul>
            </SpaceBetween>
          </Alert>
        )}
      </SpaceBetween>
    </Container>
  );
};

VNCViewer.propTypes = {
  vpId: PropTypes.string.isRequired,
  vncEndpoint: PropTypes.string.isRequired,
  websocketUrl: PropTypes.string.isRequired,
  status: PropTypes.string,
  manualActionType: PropTypes.string,
  manualActionMessage: PropTypes.string,
  manualActionTimeoutSeconds: PropTypes.number,
  manualActionStartTime: PropTypes.string,
  compact: PropTypes.bool,
  onOpenNewTab: PropTypes.func,
  showHeader: PropTypes.bool,
};

VNCViewer.defaultProps = {
  status: null,
  manualActionType: null,
  manualActionMessage: null,
  manualActionTimeoutSeconds: null,
  manualActionStartTime: null,
  compact: false,
  onOpenNewTab: null,
  showHeader: true,
};

export default VNCViewer;
