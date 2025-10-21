# Add noVNC Support for Real-Time Virtual Participant Viewing and Interaction

## Overview

This PR implements browser-based VNC access to LMA Virtual Participants, enabling users to view and interact with the virtual participant in real-time for CAPTCHA handling and manual interventions during meetings.

## Problem Statement

Virtual participants can encounter CAPTCHAs or other interactive elements during meeting joins that require human intervention. Previously, there was no way to view or interact with the virtual participant's browser session, making it impossible to handle these scenarios.

## Solution

Implemented noVNC (browser-based VNC) with **CloudFront + Application Load Balancer** architecture with dynamic target registration:

```
Browser (noVNC client)
  ↓ WSS (CloudFront default SSL certificate)
  ↓
CloudFront Distribution
  ↓ Path: /vnc/{vpId}
  ↓
Application Load Balancer (HTTP:80)
  ↓ Port 5901
  ↓
ECS Task (x11vnc + websockify)
  - Dynamically registers with ALB on startup
  - Waits for health check to pass
  - Signals VNC_READY only when healthy
  - Deregisters on shutdown
```

## Key Features

- ✅ **Real-time viewing** - See exactly what the virtual participant sees
- ✅ **Interactive control** - Full mouse/keyboard control for CAPTCHA handling
- ✅ **Automatic connection** - VNC viewer appears automatically when VP is ready
- ✅ **CloudFront SSL** - No certificate management required
- ✅ **Dynamic registration** - Tasks self-register with ALB on startup
- ✅ **Health checks** - Only signals ready after ALB health check passes
- ✅ **Multi-user support** - Path-based routing with vpId
- ✅ **Secure** - CloudFront IP restriction, private networking, audit logging
- ✅ **Zero manual configuration** - Everything automated via CloudFormation
- ✅ **Automatic cleanup** - Tasks deregister from ALB on shutdown

## Architecture Highlights

### Backend (ECS Container)
- Added X11 display server (Xvfb) for virtual framebuffer
- Added x11vnc with websockify for WebSocket-compatible VNC server
- Automatic VNC server startup with readiness signaling
- **Dynamic ALB registration** - Task registers its private IP with target group
- **Health check wait** - Waits for ALB health check before signaling ready
- **Automatic deregistration** - Cleanup on normal and signal-based shutdown

### Infrastructure (CloudFormation)

**AI Stack** (lma-ai-stack):
- **CloudFront Distribution** - VNC origin pointing to ALB
- **Cache Behavior** - `/vnc/*` path routes to ALB with WebSocket support
- **Application Load Balancer** - Internet-facing in public subnets
- **Target Group** - Port 5901, IP targets, HTTP health checks
- **ALB Security Group** - Restricted to CloudFront IP prefix list
- **ECS Security Group** - For Virtual Participant tasks
- **Security Group Rules** - ALB → ECS on port 5901
- **CloudFront Prefix List Lookup** - Custom resource for region-agnostic deployment

**Virtual Participant Stack** (lma-virtual-participant-stack):
- **ECS Cluster** - Fargate tasks
- **Task Definition** - Includes `VNC_TARGET_GROUP_ARN` environment variable
- **ECS Service** - DesiredCount: 0 (tasks managed by Step Functions)
- **IAM Permissions** - Task role can register/deregister ALB targets
- Uses security group and target group from AI stack

### Status Manager (TypeScript)
- `registerWithTargetGroup()` - Gets task IP from ECS metadata, registers with ALB
- `waitForTargetHealthy()` - Polls ALB health check (up to 60s, checks every 2s)
- `deregisterFromTargetGroup()` - Cleanup on shutdown
- Uses AWS SDK `ElasticLoadBalancingV2Client`

### Application Flow (index.ts)
1. VNC server starts
2. **Registers with ALB** and waits for healthy
3. Signals VNC_READY via AppSync (only after healthy)
4. Continues with meeting join
5. On shutdown: Deregisters from ALB

### UI (React)
- VNCViewer component with interactive controls
- Integrated into VirtualParticipantDetails page
- Receives VNC WebSocket URL via GraphQL subscription
- Connects to: `wss://cloudfront-domain/vnc/{vpId}`
- Automatic connection when VP is ready

## Implementation Details

### Files Added

### Files Modified

**Backend:**
- `lma-virtual-participant-stack/backend/Dockerfile` - Added VNC packages
- `lma-virtual-participant-stack/backend/entrypoint.sh` - VNC server startup
- `lma-virtual-participant-stack/backend/package.json` - Added ELBv2 SDK
- `lma-virtual-participant-stack/backend/src/status-manager.ts` - ALB registration methods
- `lma-virtual-participant-stack/backend/src/index.ts` - Registration/deregistration calls

**Infrastructure:**
- `lma-ai-stack/deployment/lma-ai-stack.yaml` - ALB, CloudFront origin, security groups
- `lma-ai-stack/source/appsync/schema.graphql` - Added VNC fields
- `lma-virtual-participant-stack/template.yaml` - Task definition, IAM permissions
- `lma-main.yaml` - Stack orchestration, parameter passing

**UI:**
- `lma-ai-stack/source/ui/package.json` - Added @novnc/novnc@1.5.0
- `lma-ai-stack/source/ui/src/components/virtual-participant-layout/VNCViewer.jsx` - New component
- `lma-ai-stack/source/ui/src/components/virtual-participant-layout/VirtualParticipantDetails.jsx` - Integration

## Technical Decisions

**CloudFront + ALB Approach (Implemented):**
- ✅ Native WebSocket support (no encoding needed)
- ✅ CloudFront provides SSL certificate
- ✅ ALB handles load balancing and health checks
- ✅ No timeout limits
- ✅ Better performance (no Lambda in data path)

**Dynamic Registration Approach (Implemented):**
- ✅ Works with existing Step Functions architecture
- ✅ Each task manages its own ALB registration
- ✅ Health checks ensure readiness
- ✅ Automatic cleanup on shutdown
- ✅ Supports multiple concurrent tasks

## Security

- **CloudFront IP Restriction** - ALB only accepts traffic from CloudFront IPs
- **Network Isolation** - ECS tasks in private subnets
- **TLS Encryption** - CloudFront default SSL certificate
- **Security Groups** - Layered security (CloudFront → ALB → ECS)
- **IAM Permissions** - Task role scoped to specific target group
- **AppSync Authorization** - GraphQL mutations require Cognito authentication
- **Audit Logging** - VNC connection events logged to CloudWatch

**Initial version without cognito**
- Users must authenticate to access the React app and create VPs via AppSync
- VPs are ephemeral - they only exist during active meetings
- WebSocket IDs are unpredictable - an attacker would need to:
  - Be authenticated in the app
  - Guess the correct WebSocket ID (VP creates it's own ALB target like wss://<cloudfront-url>/vnc/33260eca-3770-45a2-9c3d-f0ef52f64501)
  - Do so within the meeting window (before VP terminates)
- Network isolation - Only CloudFront can reach the ALB

TODO: Cognito + cloudfront for comprehensive security

## Testing

- ✅ All ESLint checks pass
- ✅ TypeScript compilation successful
- ✅ React build successful with noVNC 1.5.0
- ✅ CloudFormation templates validated
- ✅ No circular dependencies
- ✅ Manual testing: VNC connection works through CloudFront
- ✅ Health checks pass before signaling ready
- ✅ Automatic deregistration on shutdown

## Deployment

No new parameters required! Simply deploy or update the main LMA stack:

The system automatically:
1. Creates CloudFront distribution with VNC origin
2. Creates ALB with target group in public subnets
3. Creates security groups with CloudFront prefix list
4. Configures ECS tasks with ALB registration capability
5. Publishes VNC WebSocket URL via AppSync
6. UI connects automatically when VP is ready

## Usage

1. Start a virtual participant from the LMA UI
2. Navigate to Virtual Participant Details page
3. Wait 10-30 seconds for VNC server to start
4. Wait for ALB health check to pass
5. VNC viewer appears automatically with live view
6. Click inside viewer to interact
7. Handle CAPTCHAs or other manual interventions
8. Use controls: View Only, Scale to Fit, Fullscreen, Ctrl+Alt+Del

## Cost Impact

Estimated additional cost: ~$20-25/month
- CloudFront: Data transfer charges (~$5-10/month for typical usage)
- Application Load Balancer: ~$16/month (fixed cost)
- Increased ECS memory (4GB vs 2GB): ~$0.04/hour per active task

## Breaking Changes

None - This is a new feature that doesn't affect existing functionality.

## References

- [noVNC GitHub](https://github.com/novnc/noVNC)
- [CloudFront WebSocket Support](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/distribution-working-with.websockets.html)
- [Application Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html)
- [ECS Task Networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-networking.html)
