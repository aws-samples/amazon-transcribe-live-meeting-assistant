/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
// eslint-disable-next-line import/no-unresolved
import RFB from '@novnc/novnc/core/rfb';
import { Container, Header, SpaceBetween, Alert, Spinner, Box, Button, Toggle, Badge } from '@awsui/components-react';

const VNCViewer = ({ vpId, vncEndpoint, websocketUrl }) => {
  const canvasRef = useRef(null);
  const rfbRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(null);
  const [viewOnly, setViewOnly] = useState(false);
  const [scaleViewport, setScaleViewport] = useState(true);

  useEffect(() => {
    if (!canvasRef.current || !vncEndpoint || !websocketUrl) return undefined;

    setConnecting(true);
    setError(null);

    // Use API Gateway WebSocket URL (includes AWS-managed SSL certificate)
    // Format: wss://{api-id}.execute-api.{region}.amazonaws.com/prod
    const wsUrl = websocketUrl;

    console.log('Connecting to VNC via API Gateway:', wsUrl);
    console.log('Target endpoint:', vncEndpoint);

    try {
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
      console.error('Failed to create RFB:', err);
      setError(`Failed to connect: ${err.message}`);
      setConnecting(false);
    }

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
        <Header
          variant="h2"
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
              <Button onClick={handleCtrlAltDel} disabled={!connected}>
                Ctrl+Alt+Del
              </Button>
              <Button onClick={handleRefresh} iconName="refresh">
                Reconnect
              </Button>
            </SpaceBetween>
          }
        >
          Live Virtual Participant View
          {connected && <Badge color="green">Connected</Badge>}
          {connecting && <Badge color="blue">Connecting...</Badge>}
        </Header>
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

        {connected && (
          <Alert type="success">
            <SpaceBetween direction="vertical" size="xs">
              <div>
                <strong>Connected</strong> - You can now interact with the virtual participant
              </div>
              <div>Click inside the viewer to control the virtual participant with your mouse and keyboard</div>
            </SpaceBetween>
          </Alert>
        )}

        <div
          ref={canvasRef}
          style={{
            width: '100%',
            height: '600px',
            border: '1px solid #ccc',
            backgroundColor: '#000',
            display: 'flex',
            justifyContent: 'center',
            alignItems: 'center',
            cursor: connected ? 'default' : 'wait',
          }}
        />

        <Alert type="info">
          <SpaceBetween direction="vertical" size="xs">
            <div>
              <strong>Tips for using the live view:</strong>
            </div>
            <ul style={{ margin: 0, paddingLeft: '20px' }}>
              <li>Click inside the viewer to interact with the virtual participant</li>
              <li>Use your mouse and keyboard normally to handle CAPTCHAs or other interactions</li>
              <li>Enable &quot;View Only&quot; mode to prevent accidental interactions</li>
              <li>Use &quot;Scale to Fit&quot; to adjust the display size</li>
              <li>Click &quot;Fullscreen&quot; for a larger view</li>
            </ul>
          </SpaceBetween>
        </Alert>
      </SpaceBetween>
    </Container>
  );
};

VNCViewer.propTypes = {
  vpId: PropTypes.string.isRequired,
  vncEndpoint: PropTypes.string.isRequired,
  websocketUrl: PropTypes.string.isRequired,
};

export default VNCViewer;
