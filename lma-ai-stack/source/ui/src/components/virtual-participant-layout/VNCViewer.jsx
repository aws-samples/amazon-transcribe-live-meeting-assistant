/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * ---------------------------------------------------------------------------
 * Third-party notice (MPL 2.0 Section 3.2(a)):
 *
 * This component imports and distributes (as part of the bundled/minified
 * JavaScript served to end-user browsers) the noVNC library
 * (@novnc/novnc), which is licensed under the Mozilla Public License,
 * v. 2.0 (MPL-2.0).
 *
 * The Source Code Form of the noVNC covered files can be obtained from
 * the upstream project at:
 *
 *     https://github.com/novnc/noVNC
 *
 * A copy of the MPL-2.0 license text is included in the THIRD-PARTY-LICENSES.txt
 * file at the root of this project, and is also available at
 * https://www.mozilla.org/MPL/2.0/. This project does not modify any
 * noVNC source files; noVNC is consumed as an unmodified library dependency.
 * ---------------------------------------------------------------------------
 */
import React, { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
// noVNC (@novnc/novnc) is distributed under the Mozilla Public License, v. 2.0.
// Source: https://github.com/novnc/noVNC. See THIRD-PARTY-LICENSES.txt.
// noVNC v1.7.0 exposes the RFB module via the package-root "exports" string
// (./core/rfb.js). The eslint-plugin-import resolver does not understand this
// bare-string exports map, so disable the false-positive unresolved error.
// eslint-disable-next-line import/no-unresolved
import RFB from '@novnc/novnc';
import { generateClient } from 'aws-amplify/api';
import {
  Container,
  Header,
  SpaceBetween,
  Alert,
  Spinner,
  Box,
  Button,
  Toggle,
  Badge,
} from '@cloudscape-design/components';
import { buildVncConnection, isMicrovmEndpoint, fetchMicrovmAuthToken, fetchVncEdgeToken } from './vncConnection';

// Used to mint the short-lived VNC token for whichever transport is in play.
const gqlClient = generateClient();

// Give up auto-reconnecting after this many consecutive failed attempts.
const MAX_RECONNECT_ATTEMPTS = 8;
// A connection that held at least this long counts as having worked, so the
// next drop starts the backoff over instead of continuing it.
const STABLE_CONNECTION_MS = 30000;

const VNCViewer = ({
  vpId,
  vncEndpoint,
  status = null,
  manualActionType = null,
  manualActionMessage = null,
  manualActionTimeoutSeconds = null,
  manualActionStartTime = null,
  compact = false,
  onOpenNewTab = null,
  showHeader = true,
}) => {
  const canvasRef = useRef(null);
  const rfbRef = useRef(null);
  // The connection attempt the effect below currently owns: { cancelled, ended,
  // rfb }. Each run of the effect creates its own and cancels the previous one
  // in its cleanup. Events from a cancelled session are ignored, because noVNC
  // reports `disconnect` asynchronously (after the WebSocket's own close event,
  // or after a 3 s timeout), which is after the next session has started.
  const sessionRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const reconnectAttemptsRef = useRef(0);
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState(null);
  const [viewOnly, setViewOnly] = useState(true);
  const [scaleViewport, setScaleViewport] = useState(true);
  // Read by the connection effect when it creates a client, so that changing
  // either toggle updates the live client instead of reconnecting.
  const viewOnlyRef = useRef(viewOnly);
  const scaleViewportRef = useRef(scaleViewport);
  const [manualActionTimeRemaining, setManualActionTimeRemaining] = useState(0);
  // Bumping this forces the connection effect to tear down and reconnect. The
  // Reconnect button and auto-reconnect logic both drive it.
  const [reconnectNonce, setReconnectNonce] = useState(0);

  // Determine if manual action is required based on props
  const manualActionRequired = status === 'MANUAL_ACTION_REQUIRED' && manualActionType;

  // Automatically disable viewOnly when manual action is required
  useEffect(() => {
    if (manualActionRequired && viewOnly === true) {
      setViewOnly(false);
    }
  }, [manualActionRequired]);

  // Apply the toggles to the live client. noVNC supports changing both on a
  // connected client, so neither is a reason to reconnect.
  useEffect(() => {
    viewOnlyRef.current = viewOnly;
    if (rfbRef.current) {
      rfbRef.current.viewOnly = viewOnly;
    }
  }, [viewOnly]);

  useEffect(() => {
    scaleViewportRef.current = scaleViewport;
    if (rfbRef.current) {
      rfbRef.current.scaleViewport = scaleViewport;
    }
  }, [scaleViewport]);

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

  // One connection per viewer session. Only the VP, its endpoint, and an
  // explicit reconnect (button, backoff timer, returning to the tab) start a
  // new one; status updates and the View Only / Scale to Fit toggles do not.
  useEffect(() => {
    if (!canvasRef.current || !vpId || !vncEndpoint) return undefined;

    const session = { cancelled: false, ended: false, rfb: null };
    sessionRef.current = session;
    setConnecting(true);
    setError(null);

    // Mint a token for this session, then connect
    const connectWithAuth = async () => {
      try {
        // Two transports, depending on VPLaunchType (see vncConnection.js). Both
        // tokens are minted by an authenticated resolver that checks the
        // caller's access to this VP, are scoped to that VP's noVNC port, and
        // are short-lived, so each is minted per connection attempt:
        //  - ECS: wss://<cloudfront>/vnc/<vpId>?token=<signed edge token>
        //  - MicroVM: the VM's own endpoint, with a port-scoped token passed as
        //    a WebSocket subprotocol (browsers can't set the X-aws-proxy-auth
        //    header that Lambda would otherwise expect).
        let authToken;
        let edgeToken;
        // The VNC server asks for its own credential; the ECS mint returns it
        // with the token so this stays one round trip.
        let vncPassword = '';
        if (isMicrovmEndpoint(vncEndpoint)) {
          authToken = await fetchMicrovmAuthToken(gqlClient, vpId);
        } else {
          const minted = await fetchVncEdgeToken(gqlClient, vpId);
          edgeToken = minted.token;
          vncPassword = minted.vncPassword || '';
        }
        // Torn down while the token was being minted: connecting now would
        // leave a client nothing owns.
        if (session.cancelled) return;
        const { url: wsUrl, wsProtocols } = buildVncConnection({
          endpoint: vncEndpoint,
          edgeToken,
          authToken,
        });

        console.log(
          isMicrovmEndpoint(vncEndpoint)
            ? 'Connecting to VNC via the MicroVM endpoint'
            : 'Connecting to VNC via CloudFront with authentication',
        );
        console.log('Virtual Participant ID:', vpId);

        const rfb = new RFB(canvasRef.current, wsUrl, {
          credentials: { password: vncPassword },
          // noVNC 1.7 forwards this to `new WebSocket(url, protocols)`.
          ...(wsProtocols.length > 0 ? { wsProtocols } : {}),
        });
        session.rfb = rfb;

        // Configure RFB
        rfb.scaleViewport = scaleViewportRef.current;
        rfb.resizeSession = false;
        rfb.viewOnly = viewOnlyRef.current;

        let connectedAt = null;

        // Event handlers
        rfb.addEventListener('connect', () => {
          if (session.cancelled) return;
          console.log('VNC connected successfully');
          connectedAt = Date.now();
          setConnected(true);
          setConnecting(false);
          setReconnecting(false);
          setError(null);
        });

        rfb.addEventListener('disconnect', (e) => {
          // A session this effect already tore down (re-run, unmount, manual
          // reconnect). The run that replaced it owns the connection now.
          if (session.cancelled) return;
          console.log('VNC disconnected:', e.detail);
          session.ended = true;
          setConnected(false);
          setConnecting(false);
          if (rfbRef.current === rfb) {
            rfbRef.current = null;
          }

          // Unexpected drop. This includes "clean" closes that happen when the
          // browser idles or the tab is backgrounded (the proxy/websocket gets
          // torn down), which is the black-screen case. Auto-reconnect with
          // exponential backoff up to a cap. The backoff only starts over after
          // a connection has held for a while, so a server that accepts and then
          // drops at once is retried with growing delays rather than every second.
          if (connectedAt !== null && Date.now() - connectedAt >= STABLE_CONNECTION_MS) {
            reconnectAttemptsRef.current = 0;
          }
          if (reconnectAttemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
            setReconnecting(false);
            setError('Connection lost. The virtual participant may have ended. Use Reconnect to try again.');
            return;
          }

          const attempt = reconnectAttemptsRef.current + 1;
          reconnectAttemptsRef.current = attempt;
          const delayMs = Math.min(1000 * 2 ** (attempt - 1), 15000);
          console.log(`VNC reconnecting (attempt ${attempt}/${MAX_RECONNECT_ATTEMPTS}) in ${delayMs}ms`);
          setReconnecting(true);
          setError(null);
          if (reconnectTimerRef.current) {
            clearTimeout(reconnectTimerRef.current);
          }
          reconnectTimerRef.current = setTimeout(() => {
            reconnectTimerRef.current = null;
            setReconnectNonce((n) => n + 1);
          }, delayMs);
        });

        rfb.addEventListener('securityfailure', (e) => {
          if (session.cancelled) return;
          console.error('VNC security failure:', e.detail);
          setError(`Security failure: ${e.detail.reason}`);
          setConnecting(false);
        });

        rfb.addEventListener('credentialsrequired', () => {
          if (session.cancelled) return;
          console.log('VNC credentials required');
          setError('Authentication required');
          setConnecting(false);
        });

        rfbRef.current = rfb;
      } catch (err) {
        if (session.cancelled) return;
        session.ended = true;
        console.error('Failed to connect:', err);
        setError(`Failed to connect: ${err.message}`);
        setConnecting(false);
      }
    };

    // Call the async function
    connectWithAuth();

    return () => {
      // Cancel first, so the disconnect event noVNC fires later for this
      // client is recognised as ours and does not queue a reconnect.
      session.cancelled = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (session.rfb) {
        session.rfb.disconnect();
      }
      if (rfbRef.current === session.rfb) {
        rfbRef.current = null;
      }
    };
  }, [vpId, vncEndpoint, reconnectNonce]);

  // When the user returns to the tab/window after it was backgrounded, the VNC
  // connection has often been silently dropped (going black). If the current
  // session has ended, reconnect right away instead of waiting on backoff. A
  // session that is still connected, or still connecting, is left alone.
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState !== 'visible') return;
      const { current } = sessionRef;
      if (current && !current.cancelled && !current.ended) return;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      reconnectAttemptsRef.current = 0;
      setReconnectNonce((n) => n + 1);
    };

    document.addEventListener('visibilitychange', handleVisibility);
    window.addEventListener('focus', handleVisibility);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibility);
      window.removeEventListener('focus', handleVisibility);
    };
  }, []);

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
    // Cancel any pending auto-reconnect and reset backoff so a manual click
    // always tries immediately. Bumping the nonce re-runs the connection
    // effect, whose cleanup tears down the current client.
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    reconnectAttemptsRef.current = 0;
    setError(null);
    setReconnecting(false);
    setReconnectNonce((n) => n + 1);
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
            {connecting && !reconnecting && <Badge color="blue">Connecting...</Badge>}
            {reconnecting && <Badge color="blue">Reconnecting...</Badge>}
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

        {(connecting || reconnecting) && !error && (
          <Box textAlign="center" padding="l">
            <Spinner size="large" />
            <Box margin={{ top: 's' }}>
              {reconnecting ? 'Reconnecting to virtual participant...' : 'Connecting to virtual participant...'}
            </Box>
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

        {/*
          When Scale to Fit is ON: the outer wrapper takes full width and
          uses aspect-ratio to match the 1920:1000 Puppeteer viewport.
          The canvas div inside it is explicitly sized to 100%/100% (NOT
          flex) so noVNC's scaleViewport scales the source to fill the
          full visible area. Without this, flex+flex-start was leaving
          gaps that prevented the right column of wide Zoom layouts from
          rendering.

          When Scale to Fit is OFF: let the viewer grow naturally and
          scroll horizontally / vertically.
        */}
        <div
          style={
            scaleViewport
              ? {
                  width: '100%',
                  // Source viewport is 1920x1000 (see
                  // lma-virtual-participant-stack/backend/src/index.ts).
                  aspectRatio: '1920 / 1000',
                  // Cap height so it doesn't dominate tall windows.
                  maxHeight: compact ? '50vh' : '75vh',
                  overflow: 'hidden',
                  border: '1px solid #ccc',
                  backgroundColor: '#000',
                  // Centre when maxHeight kicks in (wide window) so the
                  // letterboxing is symmetrical.
                  display: 'flex',
                  justifyContent: 'center',
                }
              : {}
          }
        >
          <div
            ref={canvasRef}
            style={{
              // 100% / 100% under scaleViewport so noVNC's scaleViewport
              // sees a real bounding box equal to the wrapper. Without
              // this the inner div was sized to its natural content and
              // didn't fill the wrapper, leaving the source clipped.
              width: scaleViewport ? '100%' : '100%',
              height: (() => {
                if (scaleViewport) return '100%';
                return compact ? '300px' : '800px';
              })(),
              border: scaleViewport ? 'none' : '1px solid #ccc',
              backgroundColor: '#000',
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
              <Box fontSize="body-s" color="text-body-secondary">
                This viewer uses the{' '}
                <a href="https://github.com/novnc/noVNC" target="_blank" rel="noopener noreferrer">
                  noVNC
                </a>{' '}
                library, distributed under the{' '}
                <a href="https://www.mozilla.org/MPL/2.0/" target="_blank" rel="noopener noreferrer">
                  Mozilla Public License 2.0
                </a>
                . Source code for noVNC is available at the link above.
              </Box>
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
  status: PropTypes.string,
  manualActionType: PropTypes.string,
  manualActionMessage: PropTypes.string,
  manualActionTimeoutSeconds: PropTypes.number,
  manualActionStartTime: PropTypes.string,
  compact: PropTypes.bool,
  onOpenNewTab: PropTypes.func,
  showHeader: PropTypes.bool,
};

export default VNCViewer;
