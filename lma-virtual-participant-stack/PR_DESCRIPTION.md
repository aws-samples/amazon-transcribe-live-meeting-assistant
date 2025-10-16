# Add noVNC Support for Real-Time Virtual Participant Viewing and Interaction

## Overview

This PR implements browser-based VNC access to LMA Virtual Participants, enabling users to view and interact with the virtual participant in real-time for CAPTCHA handling and manual interventions during meetings.

## Problem Statement

Virtual participants can encounter CAPTCHAs or other interactive elements during meeting joins that require human intervention. Previously, there was no way to view or interact with the virtual participant's browser session, making it impossible to handle these scenarios.

## Solution

Implemented noVNC (browser-based VNC) with **API Gateway WebSocket + Lambda Proxy** architecture:

```
Browser (noVNC client)
  ↓ WSS (AWS-managed SSL certificate)
  ↓
API Gateway WebSocket API
  ↓ Lambda Integration
  ↓
Lambda Function (WebSocket Proxy)
  ↓ TCP Socket (VPC)
  ↓
ECS Task (x11vnc + websockify)
```

## Key Features

- ✅ **Real-time viewing** - See exactly what the virtual participant sees
- ✅ **Interactive control** - Full mouse/keyboard control for CAPTCHA handling
- ✅ **Automatic connection** - VNC viewer appears automatically when VP is ready
- ✅ **AWS-managed SSL** - No certificate management required
- ✅ **Serverless proxy** - Lambda handles WebSocket connections, pay per use
- ✅ **Secure** - IAM authorization, private networking, audit logging
- ✅ **Zero manual configuration** - Everything automated via CloudFormation
- ✅ **No load balancers needed** - Simpler architecture, lower cost

## Architecture Highlights

### Backend (ECS Container)
- Added X11 display server (Xvfb) for virtual framebuffer
- Added x11vnc with websockify for WebSocket-compatible VNC server
- Automatic VNC server startup with readiness signaling
- Publishes VNC endpoint (private IP) via AppSync when ready

### Infrastructure (CloudFormation)
- **API Gateway WebSocket API** - Provides AWS-managed SSL certificate
- **Lambda WebSocket Proxy** - Forwards WebSocket frames to VNC server via TCP
- **DynamoDB Connections Table** - Stores WebSocket connection state with TTL
- **VPC Networking** - Lambda deployed in VPC to reach ECS tasks
- **Security Groups** - Lambda can connect to ECS tasks on port 5901
- **No circular dependencies** - Clean stack architecture

### Lambda Proxy Function
- **Runtime**: Python 3.12
- **Timeout**: 900 seconds (15 minutes for long sessions)
- **Memory**: 512 MB
- **VPC**: Deployed in private subnets
- **Routes**:
  - `$connect`: Validates vpId, retrieves VNC endpoint, stores connection state
  - `$default`: Proxies WebSocket frames bidirectionally via TCP socket
  - `$disconnect`: Cleans up connection state

### UI (React)
- New VNCViewer component with interactive controls
- Integrated into VirtualParticipantDetails page
- Receives VNC WebSocket URL via GraphQL subscription
- Automatic connection when VP is ready
- Passes vpId as query parameter for Lambda routing

## Implementation Details

### Files Added

**Lambda:**
- `lma-virtual-participant-stack/lambda/vnc-websocket-proxy.py` - WebSocket proxy function

**Documentation:**
- `lma-virtual-participant-stack/NOVNC_VERSION_NOTE.md` - noVNC version compatibility notes

### Files Modified

**Backend:**
- `lma-virtual-participant-stack/backend/Dockerfile` - Added VNC packages
- `lma-virtual-participant-stack/backend/entrypoint.sh` - VNC server startup
- `lma-virtual-participant-stack/backend/src/status-manager.ts` - VNC signaling
- `lma-virtual-participant-stack/backend/src/index.ts` - VNC integration

**Infrastructure:**
- `lma-ai-stack/source/appsync/schema.graphql` - Added VNC fields
- `lma-virtual-participant-stack/template.yaml` - Lambda + API Gateway resources
- `lma-cognito-stack/deployment/lma-cognito-stack.yaml` - Added outputs
- `lma-main.yaml` - Stack orchestration

**UI:**
- `lma-ai-stack/source/ui/package.json` - Added @novnc/novnc@1.5.0 (pinned)
- `lma-ai-stack/source/ui/src/components/virtual-participant-layout/VNCViewer.jsx` - New component
- `lma-ai-stack/source/ui/src/components/virtual-participant-layout/VirtualParticipantDetails.jsx` - Integration

**Documentation:**
- `lma-virtual-participant-stack/NOVNC_IMPLEMENTATION.md` - Technical details
- `lma-virtual-participant-stack/DEPLOYMENT_GUIDE.md` - Deployment instructions
- `lma-virtual-participant-stack/IMPLEMENTATION_SUMMARY.md` - Project overview

## Technical Decisions

### Why Lambda Proxy Instead of Load Balancer?

**Initial Approach (Rejected):**
- API Gateway WebSocket + VPC Link + NLB
- **Problem**: VPC Links V2 don't support WebSocket APIs (only HTTP APIs)

**Alternative Considered (Rejected):**
- Application Load Balancer with Cognito authentication
- **Problem**: Requires SSL certificate management, more complex, higher cost

**Final Approach (Implemented):**
- API Gateway WebSocket + Lambda Proxy
- **Benefits**:
  - ✅ No load balancer needed (simpler, cheaper)
  - ✅ No SSL certificate management (API Gateway provides it)
  - ✅ Serverless (pay per connection)
  - ✅ Fully supported by AWS (no workarounds)

### Why noVNC 1.5.0 Instead of 1.6.0?

noVNC 1.6.0 introduced a top-level `await` in `lib/util/browser.js` that breaks webpack/babel bundling. Version 1.5.0 is the latest stable version compatible with react-scripts. See `NOVNC_VERSION_NOTE.md` for details.

## Security

- **IAM Authorization** - API Gateway $connect route validates IAM credentials
- **Network Isolation** - Lambda and ECS tasks in private subnets
- **TLS Encryption** - AWS-managed SSL certificate on API Gateway
- **Audit Logging** - VNC connection events logged to CloudWatch
- **AppSync Authorization** - GraphQL mutations require Cognito authentication
- **Connection State** - DynamoDB with TTL for automatic cleanup

## Testing

- ✅ All ESLint checks pass
- ✅ TypeScript compilation successful
- ✅ React build successful with noVNC 1.5.0
- ✅ CloudFormation templates validated
- ✅ No circular dependencies
- ✅ Documentation complete

## Deployment

No new parameters required! Simply deploy or update the main LMA stack:

```bash
aws cloudformation deploy \
  --template-file lma-main.yaml \
  --stack-name your-lma-stack \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND
```

The system automatically:
1. Creates API Gateway WebSocket API with AWS-managed SSL
2. Deploys Lambda proxy function in VPC
3. Creates DynamoDB connections table
4. Configures IAM authorization
5. Publishes WebSocket URL via AppSync
6. UI connects automatically when VP is ready

## Usage

1. Start a virtual participant from the LMA UI
2. Navigate to Virtual Participant Details page
3. Wait 10-30 seconds for VNC server to start
4. VNC viewer appears automatically with live view
5. Click inside viewer to interact
6. Handle CAPTCHAs or other manual interventions
7. Use controls: View Only, Scale to Fit, Fullscreen, Ctrl+Alt+Del

## Cost Impact

Estimated additional cost: ~$5-10/month
- API Gateway WebSocket: Pay per connection/message (~$1-3/month)
- Lambda: Pay per invocation (~$2-5/month for typical usage)
- DynamoDB: Pay per request (minimal, <$1/month)
- Increased ECS memory (4GB vs 2GB): ~$0.04/hour per active task

**Cost savings vs ALB approach:** ~$10/month (no NLB/ALB charges)

## Breaking Changes

None - This is a new feature that doesn't affect existing functionality.

## Future Enhancements

- [ ] Add Cognito authentication to WebSocket API (currently IAM only)
- [ ] Implement connection pooling for better performance
- [ ] Add session recording capability
- [ ] Support for multiple concurrent viewers per VP

## Documentation

Complete documentation available in:
- `lma-virtual-participant-stack/NOVNC_IMPLEMENTATION.md` - Technical implementation
- `lma-virtual-participant-stack/DEPLOYMENT_GUIDE.md` - Deployment steps
- `lma-virtual-participant-stack/IMPLEMENTATION_SUMMARY.md` - High-level overview
- `lma-virtual-participant-stack/NOVNC_VERSION_NOTE.md` - Version compatibility notes

## References

- [noVNC GitHub](https://github.com/novnc/noVNC)
- [API Gateway WebSocket APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-websocket-api.html)
- [Lambda VPC Networking](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html)
