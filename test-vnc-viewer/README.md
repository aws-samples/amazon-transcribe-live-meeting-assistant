# LMA Virtual Participant - VNC Test Viewer

A minimal React app for testing noVNC connections to the LMA Virtual Participant container running locally in Docker.

## Purpose

This test app validates that:
1. x11vnc WebSocket support is working correctly
2. noVNC can connect directly to x11vnc (no proxy needed)
3. The VNC connection works before deploying to AWS

## Quick Start

### 1. Install Dependencies
```bash
cd test-vnc-viewer
npm install
```

### 2. Start the Docker Container Locally

In a separate terminal, run the virtual participant container:

```bash
cd lma-virtual-participant-stack/backend

# Build the Docker image
docker build -t lma-vp-test .

# Run the container with port mapping
docker run -p 5901:5901 \
  -e AWS_REGION=us-east-1 \
  -e GRAPHQL_ENDPOINT=http://localhost:4000/graphql \
  -e VIRTUAL_PARTICIPANT_ID=test-vp-123 \
  -e MEETING_PLATFORM=zoom \
  -e MEETING_ID=test-meeting \
  -e MEETING_NAME="Test Meeting" \
  lma-vp-test
```

**Note**: The container will fail to join an actual meeting (no valid meeting URL), but x11vnc will start successfully and be ready for connections.

### 3. Start the Test Viewer

```bash
npm start
```

This will open http://localhost:3000 in your browser.

### 4. Connect to VNC

1. The default URL `ws://localhost:5901` should already be filled in
2. Click **Connect**
3. You should see the virtual participant's desktop (Xvfb with Fluxbox)

## What You Should See

### Successful Connection
```
[timestamp] Connecting to: ws://localhost:5901
[timestamp] ✓ VNC connected successfully!
```

You'll see a black/gray desktop with the Fluxbox window manager. This confirms:
- ✅ x11vnc is running with WebSocket support
- ✅ noVNC can connect directly (no proxy needed)
- ✅ The binary VNC protocol works over WebSocket

### Connection Logs in Docker

In the Docker container terminal, you should see:
```
✓ VNC WebSocket server is ready on port 5901
```

## Testing Different Scenarios

### Test 1: Local Docker (Default)
- URL: `ws://localhost:5901`
- Tests: Basic x11vnc WebSocket functionality

### Test 2: ECS Task with Public IP
1. Deploy the updated CloudFormation stack
2. Whitelist your IP in the security group
3. Start a virtual participant
4. Get the public IP from AppSync or container logs
5. Update URL to: `ws://<public-ip>:5901`
6. Click Connect

### Test 3: With ALB (Future)
- URL: `wss://<alb-dns>:443`
- Tests: Production setup with TLS

## Troubleshooting

### Connection Refused
```
✗ Failed to create RFB: WebSocket connection failed
```

**Solutions:**
- Check Docker container is running: `docker ps`
- Verify port mapping: `-p 5901:5901`
- Check x11vnc logs in container: `docker logs <container-id>`

### WebSocket Closes Immediately
```
VNC disconnected. Clean: false
```

**Solutions:**
- Verify x11vnc started with `-httpport 5901` flag
- Check container logs for x11vnc errors
- Ensure x11vnc is listening: `netstat -tuln | grep 5901` (inside container)

### Black Screen / No Display
```
Connected but nothing visible
```

**Solutions:**
- Xvfb may not have started - check container logs
- Try clicking "Scale to Fit" toggle
- Check browser console for errors

## Features

- ✅ **Connect/Disconnect** - Manual connection control
- ✅ **View Only Mode** - Prevent accidental interactions
- ✅ **Scale to Fit** - Adjust display size
- ✅ **Ctrl+Alt+Del** - Send special key combination
- ✅ **Connection Logs** - Real-time connection status
- ✅ **Error Handling** - Clear error messages

## Architecture

```
┌─────────────────────┐
│   React Test App    │
│   (localhost:3000)  │
└──────────┬──────────┘
           │
           │ ws://localhost:5901
           │ (noVNC binary protocol)
           ▼
┌─────────────────────┐
│  Docker Container   │
│                     │
│  ┌──────────────┐  │
│  │   x11vnc     │  │
│  │  port 5901   │  │
│  │  WebSocket   │  │
│  └──────────────┘  │
│         │          │
│  ┌──────▼──────┐  │
│  │    Xvfb     │  │
│  │   :99       │  │
│  └─────────────┘  │
└─────────────────────┘
```

## Next Steps After Validation

Once you've confirmed the connection works locally:

1. ✅ Deploy updated CloudFormation stack to AWS
2. ✅ Whitelist your IP in the security group
3. ✅ Start a virtual participant in ECS
4. ✅ Test connection to ECS task's public IP
5. ⏭️ Plan migration to ALB for production

## Files in This Test App

```
test-vnc-viewer/
├── package.json          # Dependencies (React 17, noVNC 1.5.0)
├── public/
│   └── index.html       # HTML template
├── src/
│   ├── index.js         # React entry point
│   ├── index.css        # Global styles
│   ├── App.js           # Main component (VNC viewer)
│   └── App.css          # Component styles
└── README.md            # This file
```

## Comparison with Production UI

This test app uses the **exact same noVNC integration** as your production UI:
- Same `@novnc/novnc` version (1.5.0)
- Same RFB import: `import RFB from '@novnc/novnc/lib/rfb'`
- Same connection pattern
- Same event handlers

The only difference is the simplified UI (no AWS Amplify UI components).

## Clean Up

When done testing:
```bash
# Stop the React app (Ctrl+C)

# Stop and remove Docker container
docker stop <container-id>
docker rm <container-id>

# Optional: Remove test app
cd ..
rm -rf test-vnc-viewer
