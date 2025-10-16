import React, { useEffect, useRef, useState } from 'react';
import RFB from '@novnc/novnc/lib/rfb';
import './App.css';

function App() {
  const canvasRef = useRef(null);
  const rfbRef = useRef(null);
  const [vncUrl, setVncUrl] = useState('ws://localhost:5901');
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(null);
  const [viewOnly, setViewOnly] = useState(false);
  const [scaleViewport, setScaleViewport] = useState(true);
  const [logs, setLogs] = useState([]);

  const addLog = (message, type = 'info') => {
    const timestamp = new Date().toLocaleTimeString();
    setLogs(prev => [...prev, { timestamp, message, type }]);
    console.log(`[${type}] ${message}`);
  };

  const handleConnect = () => {
    if (!canvasRef.current) return;

    setConnecting(true);
    setError(null);
    addLog(`Connecting to: ${vncUrl}`, 'info');

    try {
      const rfb = new RFB(canvasRef.current, vncUrl, {
        credentials: { password: '' },
      });

      rfb.scaleViewport = scaleViewport;
      rfb.resizeSession = false;
      rfb.viewOnly = viewOnly;

      rfb.addEventListener('connect', () => {
        addLog('✓ VNC connected successfully!', 'success');
        setConnected(true);
        setConnecting(false);
        setError(null);
      });

      rfb.addEventListener('disconnect', (e) => {
        addLog(`VNC disconnected. Clean: ${e.detail.clean}`, e.detail.clean ? 'info' : 'error');
        setConnected(false);
        setConnecting(false);
        if (e.detail.clean === false) {
          setError('Connection lost');
        }
      });

      rfb.addEventListener('securityfailure', (e) => {
        addLog(`✗ Security failure: ${e.detail.reason}`, 'error');
        setError(`Security failure: ${e.detail.reason}`);
        setConnecting(false);
      });

      rfb.addEventListener('credentialsrequired', () => {
        addLog('✗ Credentials required', 'error');
        setError('Authentication required');
        setConnecting(false);
      });

      rfbRef.current = rfb;
    } catch (err) {
      addLog(`✗ Failed to create RFB: ${err.message}`, 'error');
      setError(`Failed to connect: ${err.message}`);
      setConnecting(false);
    }
  };

  const handleDisconnect = () => {
    if (rfbRef.current) {
      addLog('Disconnecting...', 'info');
      rfbRef.current.disconnect();
      rfbRef.current = null;
    }
  };

  const handleCtrlAltDel = () => {
    if (rfbRef.current) {
      rfbRef.current.sendCtrlAltDel();
      addLog('Sent Ctrl+Alt+Del', 'info');
    }
  };

  useEffect(() => {
    addLog('Test VNC Viewer loaded. Ready to connect.', 'success');
  }, []);

  return (
    <div className="App">
      <div className="container">
        <h1>LMA Virtual Participant - VNC Test Viewer</h1>
        
        <div className="controls">
          <div className="input-group">
            <label>VNC WebSocket URL:</label>
            <input
              type="text"
              value={vncUrl}
              onChange={(e) => setVncUrl(e.target.value)}
              disabled={connected || connecting}
              placeholder="ws://localhost:5901"
            />
            <small>For local Docker: ws://localhost:5901 | For ECS: ws://&lt;public-ip&gt;:5901</small>
          </div>

          <div className="button-group">
            <button 
              onClick={handleConnect} 
              disabled={connected || connecting}
              className="btn-primary"
            >
              {connecting ? 'Connecting...' : 'Connect'}
            </button>
            <button 
              onClick={handleDisconnect} 
              disabled={!connected}
              className="btn-secondary"
            >
              Disconnect
            </button>
            <button 
              onClick={handleCtrlAltDel} 
              disabled={!connected}
              className="btn-secondary"
            >
              Ctrl+Alt+Del
            </button>
          </div>

          <div className="toggle-group">
            <label>
              <input
                type="checkbox"
                checked={viewOnly}
                onChange={(e) => {
                  setViewOnly(e.target.checked);
                  if (rfbRef.current) {
                    rfbRef.current.viewOnly = e.target.checked;
                  }
                }}
                disabled={!connected}
              />
              View Only
            </label>
            <label>
              <input
                type="checkbox"
                checked={scaleViewport}
                onChange={(e) => {
                  setScaleViewport(e.target.checked);
                  if (rfbRef.current) {
                    rfbRef.current.scaleViewport = e.target.checked;
                  }
                }}
                disabled={!connected}
              />
              Scale to Fit
            </label>
          </div>
        </div>

        {error && (
          <div className="alert alert-error">
            {error}
          </div>
        )}

        {connected && (
          <div className="alert alert-success">
            ✓ Connected! Click inside the viewer to interact with the virtual participant.
          </div>
        )}

        <div
          ref={canvasRef}
          className="vnc-canvas"
          style={{
            cursor: connected ? 'default' : 'wait',
          }}
        />

        <div className="logs">
          <h3>Connection Logs</h3>
          <div className="log-container">
            {logs.map((log, index) => (
              <div key={index} className={`log-entry log-${log.type}`}>
                <span className="log-time">[{log.timestamp}]</span> {log.message}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
