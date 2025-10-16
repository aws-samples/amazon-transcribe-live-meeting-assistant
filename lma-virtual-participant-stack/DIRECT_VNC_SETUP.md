# Direct VNC Connection Setup Guide

## Overview
This guide explains how to set up direct VNC connections from the browser to ECS tasks for testing and validation.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     React UI                             │
│  ┌──────────────────┐         ┌──────────────────┐     │
│  │ AppSync GraphQL  │         │  noVNC Client    │     │
│  │  (Signaling)     │         │  (VNC Stream)    │     │
│  └────────┬─────────┘         └────────┬─────────┘     │
└───────────┼──────────────────────────────┼──────────────┘
            │                              │
            │ Status Updates               │ WS Connection
            │ (public IP, port)            │ (Binary VNC)
            ▼                              ▼
    ┌───────────────┐            ┌─────────────────┐
    │   AppSync     │            │   Public IP     │
    │   GraphQL     │            │  (ECS Task)     │
    └───────┬───────┘            └────────┬────────┘
            │                              │
            │                              │ Port 5901
            ▼                              ▼
    ┌────────────────────────────────────────────┐
    │           ECS Task (Container)              │
    │                                             │
    │                         ┌─────────────┐    │
    │                         │   x11vnc    │    │
    │                         │ (port 5901) │    │
    │                         │ WebSocket   │    │
    │                         │   enabled   │    │
    │                         └─────────────┘    │
    └────────────────────────────────────────────┘
```

## Changes Made

### 1. x11vnc WebSocket Support (entrypoint.sh)
```bash
x11vnc \
    -display :99 \
    -forever \
    -shared \
    -rfbport 5901 \
    -httpport 5901 \      # Enables WebSocket support
    -http_oneport \       # Use same port for HTTP and VNC
    -nopw \
    ...
```

### 2. ECS Task Configuration (template.yaml)
- **AssignPublicIp: ENABLED** - Tasks get public IP addresses
- **Security Group** - No ingress rules by default (secure by default)

### 3. Security Group (Locked Down)
- **No ingress rules** - Must be manually whitelisted
- **Egress**: Allows all outbound (for meeting platforms, AWS APIs)

## Manual Testing Setup

### Step 1: Get Your Public IP
```bash
curl https://checkip.amazonaws.com
# Example output: 203.0.113.45
```

### Step 2: Whitelist Your IP in Security Group

**Option A: AWS Console**
1. Go to EC2 → Security Groups
2. Find security group: `{LMAStackName}-vp-sg`
3. Edit Inbound Rules → Add Rule:
   - Type: Custom TCP
   - Port: 5901
   - Source: My IP (or paste your IP/32)
   - Description: "VNC access for testing"

**Option B: AWS CLI**
```bash
# Get your IP
MY_IP=$(curl -s https://checkip.amazonaws.com)

# Get security group ID
SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=tag:Name,Values=LMA-dev-stack-vp-sg" \
  --query 'SecurityGroups[0].GroupId' \
  --output text \
  --region us-east-1)

# Add ingress rule
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp \
  --port 5901 \
  --cidr ${MY_IP}/32 \
  --region us-east-1
```

### Step 3: Start a Virtual Participant
Use your existing UI to start a virtual participant. The task will:
1. Get a public IP address
2. Start x11vnc with WebSocket support on port 5901
3. Publish the public IP via AppSync

### Step 4: Connect via noVNC
The React UI will automatically:
1. Receive the public IP from AppSync
2. Connect to `ws://<public-ip>:5901`
3. Display the live VNC session

## Connection Flow

```
1. User starts VP → ECS task launches with public IP
2. Task publishes to AppSync: { vncEndpoint: "54.123.45.67", vncPort: 5901, vncReady: true }
3. React UI receives update via AppSync subscription
4. React UI connects: ws://54.123.45.67:5901
5. x11vnc accepts WebSocket connection (no proxy needed!)
6. User sees live virtual participant screen
```

## Security Considerations

### Current Setup (Testing)
- ✅ Security group locked down by default
- ✅ Manual IP whitelisting required
- ✅ Ephemeral tasks (only live during meetings)
- ⚠️ No TLS (using `ws://` not `wss://`)
- ⚠️ Public IP exposure

### Production Recommendations
When ready for production, migrate to ALB:
1. Add Application Load Balancer
2. Use ACM certificate for TLS (`wss://`)
3. Remove public IPs from tasks
4. ALB handles routing and health checks
5. More scalable for multiple users

## Troubleshooting

### Connection Refused
- Check security group has your IP whitelisted
- Verify task has public IP: `aws ecs describe-tasks ...`
- Check x11vnc is running: View container logs

### WebSocket Closes Immediately
- Verify x11vnc started with `-httpport 5901`
- Check container logs for x11vnc errors
- Ensure port 5901 is exposed in Dockerfile

### Can't See Virtual Participant
- Check AppSync is publishing vncEndpoint
- Verify vncReady is true
- Check browser console for connection errors

## Migration to ALB (Future)

When ready for production:

1. **Keep container as-is** (x11vnc already configured)
2. **Add ALB** with target group on port 5901
3. **Add ACM certificate** to ALB
4. **Update status-manager** to publish ALB DNS
5. **Change React UI** to use `wss://` instead of `ws://`
6. **Remove public IPs** from tasks (AssignPublicIp: DISABLED)
7. **Remove manual security group rules**

The container code doesn't change - only the network path!

## Cost Comparison

### Direct Connection (Current)
- ECS Task: ~$0.04/hour
- Data Transfer: ~$0.09/GB
- **Total**: ~$0.04/hour + data

### With ALB (Production)
- ECS Task: ~$0.04/hour
- ALB: ~$0.0225/hour + $0.008/LCU-hour
- Data Transfer: ~$0.09/GB
- **Total**: ~$0.06/hour + data

The ALB adds minimal cost (~$16/month) but provides production-grade features.

## Next Steps

1. ✅ Deploy updated CloudFormation stack
2. ✅ Whitelist your IP in security group
3. ✅ Start a virtual participant
4. ✅ Test VNC connection from React UI
5. ⏭️ When validated, plan ALB migration
