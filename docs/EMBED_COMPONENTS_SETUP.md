# Embedding LMA Components in Your Application

## Overview

LMA provides an **embeddable component page** that allows you to integrate individual LMA UI components directly into your own application using iframes. This enables you to offer LMA-powered meeting features — live transcription, meeting summaries, AI chat assistants, virtual participant live views, and audio streaming — all within your own product's UI, without requiring users to navigate to the full LMA application.

The embed page is a chrome-free (no navigation sidebar, top bar, or breadcrumbs) rendering of LMA components, fully controlled via **URL query parameters** and optionally via the **postMessage Web API** for cross-origin communication.

## Why Embed LMA Components?

### Use Cases

**White-Label Meeting Intelligence**
- Embed live transcription and AI summaries directly in your customer-facing application
- Offer meeting recording and analysis as a feature of your platform
- Maintain your own branding while leveraging LMA's backend

**Custom Meeting Dashboards**
- Build dashboards that combine LMA meeting data with your own application data
- Show only the components relevant to your users (e.g., transcript only, or chat only)
- Control layout and visibility to match your UI design

**Virtual Participant Integration**
- Embed the VNC live view of a virtual participant in your meeting management UI
- Show real-time meeting transcripts alongside your own meeting controls
- Monitor virtual participant status from within your application

**Automated Meeting Workflows**
- Pre-populate meeting fields and auto-start recordings from your application
- Control meeting lifecycle (start/stop) programmatically via postMessage
- Receive meeting events (started, stopped, errors) in your parent application

## Prerequisites

- LMA deployed and accessible (v0.2.23 or later)
- Your LMA CloudFront endpoint URL (found in CloudFormation Outputs as `CloudFrontEndpoint`)
- One of the following authentication approaches configured:
  - **Cognito login** (default) — users log in via LMA's Cognito User Pool
  - **Cognito Identity Federation** — users authenticate via your IdP, federated through Cognito
  - **Token passing via postMessage** — your app obtains tokens and passes them to the iframe

## Quick Start

### 1. Basic Embed (Stream Audio)

The simplest way to get started is to embed the Stream Audio component:

```html
<iframe
  src="https://YOUR_LMA_CLOUDFRONT_URL/#/embed?component=stream-audio"
  width="100%"
  height="400px"
  style="border: none;"
  allow="microphone; display-capture"
></iframe>
```

> **Important**: The `allow="microphone; display-capture"` attribute is required for the Stream Audio component to access the user's microphone and screen audio.

### 2. Pre-Populated Stream Audio

Pre-fill the meeting form fields so users just click "Start Streaming":

```html
<iframe
  src="https://YOUR_LMA_CLOUDFRONT_URL/#/embed?component=stream-audio&meetingTopic=Sales+Call&participants=Customer&owner=agent@company.com"
  width="100%"
  height="400px"
  style="border: none;"
  allow="microphone; display-capture"
></iframe>
```

### 3. View a Meeting Transcript

Show the live transcript and AI chat for an existing meeting:

```html
<iframe
  src="https://YOUR_LMA_CLOUDFRONT_URL/#/embed?component=call-details&callId=My+Meeting+-+2025-01-29T14:30:00"
  width="100%"
  height="800px"
  style="border: none;"
></iframe>
```

### 4. Virtual Participant with VNC Live View

Show the VNC live view and transcript for a virtual participant session:

```html
<iframe
  src="https://YOUR_LMA_CLOUDFRONT_URL/#/embed?component=vp-details&vpId=abc-123-def&show=vnc,transcript"
  width="100%"
  height="900px"
  style="border: none;"
></iframe>
```

## Available Components

| Component Value | Description | Key Parameters |
|----------------|-------------|----------------|
| `stream-audio` | Full Stream Audio interface with meeting form and recording controls | `meetingTopic`, `participants`, `owner`, `autoStart` |
| `call-details` | Complete call details view (transcript + summary + chat) | `callId`, `show`, `layout` |
| `transcript` | Live meeting transcript only | `callId` |
| `summary` | Meeting summary only | `callId` |
| `chat` | Meeting Assist Bot chat only | `callId` |
| `vp-details` | Virtual participant details with selectable panels | `vpId`, `show`, `layout` |
| `vnc` | VNC live view of virtual participant only | `vpId` |
| `meeting-loader` | Blank meeting starter page (for programmatic control) | `meetingTopic`, `participants`, `owner`, `autoStart` |

## Query Parameter Reference

### Component Selection

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `component` | string | `stream-audio` | Which component to render (see table above) |
| `show` | string | (auto) | Comma-separated list of panels to display: `transcript`, `summary`, `chat`, `vnc`, `details` |
| `layout` | string | `vertical` | Layout arrangement: `vertical`, `horizontal`, `grid` |

### Meeting Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `callId` | string | — | Meeting/call ID to load for transcript, summary, or chat views |
| `vpId` | string | — | Virtual participant ID for VP-related views |
| `meetingTopic` | string | — | Pre-fill the meeting topic field |
| `participants` | string | — | Pre-fill the participant label |
| `owner` | string | (user email) | Pre-fill the meeting owner field |
| `autoStart` | boolean | `false` | Automatically start streaming when the page loads |

### Authentication

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `authMode` | string | `cognito` | Authentication mode: `cognito` (standard login) or `token` (postMessage token passing) |

## Authentication Options

### Option 1: Standard Cognito Login (Default)

The simplest approach. When the iframe loads, if the user is not already authenticated, they'll see the standard LMA Cognito login form. Once logged in, the session persists across page loads.

```html
<!-- No special auth params needed — uses default Cognito auth -->
<iframe
  src="https://YOUR_LMA_CLOUDFRONT_URL/#/embed?component=stream-audio"
  width="100%"
  height="400px"
  style="border: none;"
  allow="microphone; display-capture"
></iframe>
```

**Best for**: Internal tools, admin dashboards, cases where users already have LMA accounts.

### Option 2: Cognito Identity Federation

If your application uses its own Identity Provider (IdP), you can configure Cognito Identity Federation so your users are automatically authenticated in LMA when they're logged into your app.

**Setup Steps:**

1. In the AWS Console, navigate to your LMA Cognito User Pool
2. Add your IdP as a federated identity provider (SAML, OIDC, or Social)
3. Configure attribute mapping between your IdP and Cognito
4. Update the Cognito App Client to include your IdP
5. Users will be redirected to your IdP for login, then back to LMA

**Best for**: Enterprise SSO, organizations with existing IdP infrastructure.

### Option 3: Token Passing via postMessage

For maximum control, your application can obtain Cognito tokens directly (by calling the Cognito API) and pass them to the iframe via the `postMessage` Web API. This avoids showing any login UI in the iframe.

```html
<iframe
  id="lma-embed"
  src="https://YOUR_LMA_CLOUDFRONT_URL/#/embed?component=stream-audio&authMode=token"
  width="100%"
  height="400px"
  style="border: none;"
  allow="microphone; display-capture"
></iframe>

<script>
const iframe = document.getElementById('lma-embed');

// Listen for the iframe to signal it's ready
window.addEventListener('message', (event) => {
  if (event.data.type === 'LMA_AUTH_READY') {
    // Send authentication tokens to the iframe
    iframe.contentWindow.postMessage({
      type: 'LMA_AUTH',
      idToken: 'eyJhbGciOiJSUzI1NiIs...', // Cognito ID token
      accessToken: 'eyJhbGciOiJSUzI1NiIs...', // Cognito access token
      refreshToken: 'eyJhbGciOiJSUzI1NiIs...' // Cognito refresh token
    }, 'https://YOUR_LMA_CLOUDFRONT_URL');
  }

  if (event.data.type === 'LMA_AUTH_SUCCESS') {
    console.log('LMA iframe authenticated successfully!');
  }

  if (event.data.type === 'LMA_AUTH_ERROR') {
    console.error('LMA auth failed:', event.data.error);
  }
});
</script>
```

**Obtaining Cognito Tokens from Your Backend:**

```python
# Python example using boto3
import boto3

client = boto3.client('cognito-idp', region_name='us-east-1')

# Authenticate a user and get tokens
response = client.initiate_auth(
    ClientId='YOUR_LMA_COGNITO_CLIENT_ID',
    AuthFlow='USER_PASSWORD_AUTH',  # or USER_SRP_AUTH
    AuthParameters={
        'USERNAME': 'user@example.com',
        'PASSWORD': 'user-password'
    }
)

tokens = {
    'idToken': response['AuthenticationResult']['IdToken'],
    'accessToken': response['AuthenticationResult']['AccessToken'],
    'refreshToken': response['AuthenticationResult']['RefreshToken']
}
# Pass these tokens to your frontend, which sends them to the iframe via postMessage
```

**Best for**: Custom applications, white-label solutions, cases where you control the auth flow.

## postMessage API Reference

The embed page communicates with the parent application via the `postMessage` Web API. This enables bidirectional control and event notification.

### Messages FROM Parent → Iframe

#### Authentication

```javascript
// Send auth tokens (when authMode=token)
iframe.contentWindow.postMessage({
  type: 'LMA_AUTH',
  idToken: '...',      // Required: Cognito ID token JWT
  accessToken: '...',  // Required: Cognito access token JWT
  refreshToken: '...'  // Optional: Cognito refresh token
}, targetOrigin);

// Refresh tokens
iframe.contentWindow.postMessage({
  type: 'LMA_AUTH_REFRESH',
  idToken: '...',
  accessToken: '...',
  refreshToken: '...'
}, targetOrigin);
```

#### Meeting Control

```javascript
// Start a meeting (for stream-audio and meeting-loader components)
iframe.contentWindow.postMessage({
  type: 'LMA_START_MEETING',
  meetingTopic: 'Optional Topic',    // Override meeting topic
  participants: 'Optional Label',     // Override participant label
  owner: 'optional@email.com'        // Override owner
}, targetOrigin);

// Stop a meeting
iframe.contentWindow.postMessage({
  type: 'LMA_STOP_MEETING'
}, targetOrigin);

// Set meeting parameters (for meeting-loader component)
iframe.contentWindow.postMessage({
  type: 'LMA_SET_MEETING_PARAMS',
  meetingTopic: 'New Topic',
  participants: 'New Participant',
  owner: 'new@email.com'
}, targetOrigin);
```

### Messages FROM Iframe → Parent

#### Authentication Events

```javascript
window.addEventListener('message', (event) => {
  switch (event.data.type) {
    case 'LMA_AUTH_READY':
      // Iframe is ready to receive auth tokens
      break;
    case 'LMA_AUTH_SUCCESS':
      // Authentication was successful
      break;
    case 'LMA_AUTH_ERROR':
      // Authentication failed
      console.error(event.data.error);
      break;
  }
});
```

#### Embed Lifecycle Events

```javascript
window.addEventListener('message', (event) => {
  switch (event.data.type) {
    case 'LMA_EMBED_LOADED':
      // Embed page has loaded
      // event.data.component - which component loaded
      // event.data.params - all parsed query params
      break;
    case 'LMA_MEETING_LOADER_READY':
      // Meeting loader is ready for commands
      // event.data.state - current state (idle, waiting, etc.)
      break;
  }
});
```

#### Meeting Events

```javascript
window.addEventListener('message', (event) => {
  switch (event.data.type) {
    case 'LMA_MEETING_STARTED':
      // Meeting recording has started
      console.log('Meeting ID:', event.data.callId);
      break;
    case 'LMA_MEETING_STOPPED':
      // Meeting recording has stopped
      console.log('Meeting ID:', event.data.callId);
      break;
    case 'LMA_MEETING_ERROR':
      // An error occurred
      console.error(event.data.error);
      break;
    case 'LMA_PARAMS_SET':
      // Meeting parameters were updated successfully
      break;
  }
});
```

#### Call Details Events

```javascript
window.addEventListener('message', (event) => {
  switch (event.data.type) {
    case 'LMA_CALL_LOADED':
      // Call details loaded successfully
      // event.data.callId - the meeting ID
      // event.data.status - meeting status (IN_PROGRESS, DONE, etc.)
      break;
  }
});
```

#### Virtual Participant Events

```javascript
window.addEventListener('message', (event) => {
  switch (event.data.type) {
    case 'LMA_VP_LOADED':
      // VP details loaded
      // event.data.vpId - VP ID
      // event.data.status - VP status
      // event.data.callId - associated call ID (if available)
      break;
    case 'LMA_VP_STATUS_CHANGED':
      // VP status changed (real-time update)
      // event.data.vpId - VP ID
      // event.data.status - new status
      // event.data.callId - associated call ID
      break;
  }
});
```

## Example Integrations

### Example 1: Meeting Recording Widget

A compact widget that lets users start a meeting recording with one click:

```html
<!DOCTYPE html>
<html>
<head>
  <title>Meeting Recorder</title>
  <style>
    .recorder-widget {
      width: 600px;
      height: 350px;
      border: 1px solid #ddd;
      border-radius: 8px;
      overflow: hidden;
    }
  </style>
</head>
<body>
  <div class="recorder-widget">
    <iframe
      id="recorder"
      src="https://YOUR_LMA_URL/#/embed?component=stream-audio&meetingTopic=Quick+Recording&autoStart=false"
      width="100%"
      height="100%"
      style="border: none;"
      allow="microphone; display-capture"
    ></iframe>
  </div>

  <script>
    window.addEventListener('message', (event) => {
      if (event.data.type === 'LMA_MEETING_STARTED') {
        document.title = '🔴 Recording...';
        console.log('Recording started:', event.data.callId);
      }
      if (event.data.type === 'LMA_MEETING_STOPPED') {
        document.title = 'Meeting Recorder';
        console.log('Recording stopped:', event.data.callId);
        // You could now fetch the transcript via LMA's API
      }
    });
  </script>
</body>
</html>
```

### Example 2: Live Transcript Dashboard

Show a live transcript alongside your own meeting UI:

```html
<div style="display: flex; gap: 16px; height: 100vh;">
  <!-- Your meeting UI on the left -->
  <div style="flex: 1;">
    <h2>Your Meeting Interface</h2>
    <!-- Your custom meeting controls here -->
  </div>

  <!-- LMA transcript on the right -->
  <div style="flex: 1;">
    <iframe
      src="https://YOUR_LMA_URL/#/embed?component=transcript&callId=YOUR_MEETING_ID"
      width="100%"
      height="100%"
      style="border: none;"
    ></iframe>
  </div>
</div>
```

### Example 3: Virtual Participant Monitor

Monitor a virtual participant with VNC live view and transcript side by side:

```html
<iframe
  src="https://YOUR_LMA_URL/#/embed?component=vp-details&vpId=YOUR_VP_ID&show=vnc,transcript,chat&layout=horizontal"
  width="100%"
  height="800px"
  style="border: none;"
></iframe>
```

### Example 4: Programmatic Meeting Control

Start and stop meetings entirely from your parent application:

```html
<iframe
  id="meeting"
  src="https://YOUR_LMA_URL/#/embed?component=meeting-loader&authMode=cognito"
  width="100%"
  height="300px"
  style="border: none;"
  allow="microphone; display-capture"
></iframe>

<button onclick="startMeeting()">Start Meeting</button>
<button onclick="stopMeeting()">Stop Meeting</button>

<script>
const iframe = document.getElementById('meeting');

function startMeeting() {
  iframe.contentWindow.postMessage({
    type: 'LMA_START_MEETING',
    meetingTopic: 'Automated Meeting',
    participants: 'Team',
    owner: 'admin@company.com'
  }, '*');
}

function stopMeeting() {
  iframe.contentWindow.postMessage({
    type: 'LMA_STOP_MEETING'
  }, '*');
}

window.addEventListener('message', (event) => {
  if (event.data.type === 'LMA_MEETING_STARTED') {
    console.log('Meeting started! ID:', event.data.callId);
    // Now you could open a transcript view for this meeting
  }
});
</script>
```

### Example 5: Multi-Panel Meeting View

Show summary, transcript, and chat in a custom grid layout:

```html
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; height: 100vh;">
  <!-- Summary in top-left -->
  <iframe
    src="https://YOUR_LMA_URL/#/embed?component=summary&callId=YOUR_MEETING_ID"
    style="border: none; width: 100%; height: 100%;"
  ></iframe>

  <!-- Chat in top-right -->
  <iframe
    src="https://YOUR_LMA_URL/#/embed?component=chat&callId=YOUR_MEETING_ID"
    style="border: none; width: 100%; height: 100%;"
  ></iframe>

  <!-- Transcript spanning full width at bottom -->
  <iframe
    src="https://YOUR_LMA_URL/#/embed?component=transcript&callId=YOUR_MEETING_ID"
    style="border: none; width: 100%; height: 100%; grid-column: 1 / -1;"
  ></iframe>
</div>
```

## Layout Options

When using composite components (`call-details`, `vp-details`), you can control how panels are arranged:

### Vertical Layout (Default)
```
?layout=vertical
```
Panels stack top-to-bottom. Best for narrow containers or single-column layouts.

### Horizontal Layout
```
?layout=horizontal
```
Panels sit side-by-side. Best for wide containers. Falls back to vertical on narrow screens (< 768px).

### Grid Layout
```
?layout=grid
```
Panels arrange in an auto-fit grid (minimum 400px per column). Best for dashboards with multiple panels.

## Controlling Panel Visibility

Use the `show` parameter to control which panels appear:

```
# Show only transcript and chat
?show=transcript,chat

# Show VNC and transcript
?show=vnc,transcript

# Show everything
?show=vnc,transcript,summary,chat,details
```

**Available panels by component:**

| Component | Available Panels |
|-----------|-----------------|
| `call-details` | `transcript`, `summary`, `chat` |
| `vp-details` | `vnc`, `transcript`, `summary`, `chat`, `details` |

> **Note**: For single-purpose components (`transcript`, `summary`, `chat`, `vnc`), the `show` parameter is automatically set. You only need `show` when using `call-details` or `vp-details` and want to customize which panels appear.

## Troubleshooting

### Issue: Iframe shows login page instead of component

**Cause**: User is not authenticated in the LMA Cognito User Pool.

**Solutions**:
1. **Standard auth**: The user needs to log in via the Cognito form shown in the iframe
2. **Token auth**: Ensure you're sending valid tokens via postMessage (see [Token Passing](#option-3-token-passing-via-postmessage))
3. **Federation**: Verify your IdP is configured correctly in Cognito

### Issue: Microphone/screen capture not working in iframe

**Cause**: Missing iframe permissions.

**Solution**: Add the `allow` attribute to your iframe:
```html
<iframe
  allow="microphone; display-capture; camera"
  src="..."
></iframe>
```

For cross-origin iframes, you may also need:
```html
<iframe
  allow="microphone https://YOUR_LMA_URL; display-capture https://YOUR_LMA_URL"
  src="..."
></iframe>
```

### Issue: postMessage not received by iframe

**Cause**: Origin mismatch or timing issue.

**Solutions**:
1. Wait for the `LMA_AUTH_READY` or `LMA_EMBED_LOADED` message before sending
2. Verify the target origin matches the iframe's origin:
   ```javascript
   iframe.contentWindow.postMessage(data, 'https://YOUR_LMA_CLOUDFRONT_URL');
   ```
3. Check browser console for cross-origin errors

### Issue: "Meeting not found" when using callId

**Cause**: The callId doesn't match any meeting in LMA.

**Solutions**:
1. Verify the callId is correct — copy it from the LMA meetings list
2. Meeting IDs in LMA include the topic and timestamp (e.g., `Sales Call - 2025-01-29T14:30:00.000Z`)
3. URL-encode special characters in the callId:
   ```
   ?callId=Sales%20Call%20-%202025-01-29T14%3A30%3A00.000Z
   ```

### Issue: Components appear but data doesn't load

**Cause**: Settings not loaded from SSM Parameter Store.

**Solutions**:
1. Ensure the LMA stack is fully deployed and the settings parameter exists
2. Check that the authenticated user has permission to read SSM parameters
3. Verify the `REACT_APP_SETTINGS_PARAMETER` environment variable is set correctly

### Issue: VNC viewer shows "Connection lost"

**Cause**: Virtual participant has ended or VNC endpoint is no longer available.

**Solutions**:
1. Check the VP status — VNC is only available during active sessions
2. Verify the VP's `vncReady` field is `true`
3. Ensure the VNC WebSocket endpoint is accessible from the user's browser

### Issue: Cross-origin errors in browser console

**Cause**: CORS or Content Security Policy restrictions.

**Solutions**:
1. Ensure your LMA CloudFront distribution allows your domain in CORS headers
2. If using a Content Security Policy, add the LMA domain to `frame-src`:
   ```html
   <meta http-equiv="Content-Security-Policy"
     content="frame-src https://YOUR_LMA_CLOUDFRONT_URL;">
   ```

## Security Best Practices

1. **Always specify target origin** in postMessage calls — avoid using `'*'` in production:
   ```javascript
   iframe.contentWindow.postMessage(data, 'https://YOUR_LMA_CLOUDFRONT_URL');
   ```

2. **Validate message origins** in your parent app:
   ```javascript
   window.addEventListener('message', (event) => {
     if (event.origin !== 'https://YOUR_LMA_CLOUDFRONT_URL') return;
     // Process message...
   });
   ```

3. **Never expose tokens in URLs** — use postMessage for token passing instead of query parameters

4. **Use HTTPS** for both your application and the LMA iframe

5. **Limit iframe permissions** — only grant the permissions each component needs:
   - Stream Audio: `allow="microphone; display-capture"`
   - Transcript/Summary/Chat: no special permissions needed
   - VNC: no special permissions needed

6. **Token refresh** — if using token auth mode, implement token refresh:
   ```javascript
   setInterval(() => {
     iframe.contentWindow.postMessage({
       type: 'LMA_AUTH_REFRESH',
       idToken: freshIdToken,
       accessToken: freshAccessToken,
       refreshToken: freshRefreshToken
     }, targetOrigin);
   }, 45 * 60 * 1000); // Refresh every 45 minutes
   ```

## URL Builder Quick Reference

Base URL: `https://YOUR_LMA_CLOUDFRONT_URL/#/embed`

**Stream Audio (pre-populated):**
```
/#/embed?component=stream-audio&meetingTopic=My+Meeting&participants=Team&owner=me@co.com
```

**Auto-start Stream Audio:**
```
/#/embed?component=stream-audio&meetingTopic=Auto+Meeting&autoStart=true
```

**Full Call Details:**
```
/#/embed?component=call-details&callId=MEETING_ID
```

**Transcript Only:**
```
/#/embed?component=transcript&callId=MEETING_ID
```

**Summary Only:**
```
/#/embed?component=summary&callId=MEETING_ID
```

**Chat Only:**
```
/#/embed?component=chat&callId=MEETING_ID
```

**VP with VNC + Transcript (horizontal):**
```
/#/embed?component=vp-details&vpId=VP_ID&show=vnc,transcript&layout=horizontal
```

**VP with All Panels (grid):**
```
/#/embed?component=vp-details&vpId=VP_ID&show=vnc,transcript,summary,chat,details&layout=grid
```

**Meeting Loader (waiting for postMessage):**
```
/#/embed?component=meeting-loader
```

**Meeting Loader (with token auth):**
```
/#/embed?component=meeting-loader&authMode=token
```

## Additional Resources

- [LMA GitHub Repository](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant)
- [LMA Main README](../README.md)
- [LMA Stream Audio Documentation](../lma-ai-stack/WebUIStreamingClient.md)
- [LMA Virtual Participant Documentation](../lma-virtual-participant-stack/README.md)
- [MDN: Window.postMessage()](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage)
- [MDN: iframe allow attribute](https://developer.mozilla.org/en-US/docs/Web/HTML/Element/iframe#allow)

## Support

For issues or questions:
1. Check the [Troubleshooting](#troubleshooting) section above
2. Review browser console logs for error messages
3. Open an issue on the [LMA GitHub repository](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/issues)
