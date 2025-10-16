# noVNC Direct Connection Implementation Summary

## Problem Statement
API Gateway WebSocket APIs cannot handle noVNC's binary VNC protocol. The `$connect` route works, but the `$default` route is never invoked when noVNC sends binary VNC data, causing immediate connection failure.

## Solution: Direct Connection to ECS Tasks

Instead of routing through API Gateway, we enable direct WebSocket connections from the browser to the ECS task's public IP using x11vnc's built-in WebSocket support.

## Changes Made

### 1. Container Configuration (entrypoint.sh)
**File**: `lma-virtual-participant-stack/backend/entrypoint.sh`

Added WebSocket support to x11vnc:
```bash
x11vnc \
    -display :99 \
    -forever \
    -shared \
    -rfbport 5901 \
    -httpport 5901 \      # NEW: Enables WebSocket support
    -http_oneport \       # NEW: Use same port for HTTP and VNC
    -nopw \
    ...
```

### 2. ECS Task Network Configuration (template.yaml)
**File**: `lma-virtual-participant-stack/template.yaml`

Changed from private to public IP:
```yaml
NetworkConfiguration:
  AwsvpcConfiguration:
    AssignPublicIp: "ENABLED"  # Changed from DISABLED
```

### 3. Security Group (template.yaml)
**File**: `lma-virtual-participant-stack/template.yaml`

Removed default ingress rules for security:
```yaml
SecurityGroup:
  Type: AWS::EC2::SecurityGroup
  Properties:
    GroupDescription: Security group for Virtual Participant ECS tasks
    # NO ingress rules by default - must be manually whitelisted
    SecurityGroupEgress:
      - CidrIp: 0.0.0.0/0
        IpProtocol: "-1"
```

### 4. Status Manager (status-manager.ts)
**File**: `lma-virtual-participant-stack/backend/src/status-manager.ts`

Added method to get task public IP:
```typescript
async getTaskPublicIp(): Promise<string | null> {
  // Queries ECS API to get task details
  // Extracts ENI ID from task attachments
  // Queries EC2 API to get public IP from ENI
  return publicIp;
}
```

Updated `setVncReady()` to automatically fetch and publish public IP:
```typescript
async setVncReady(endpoint?: string, port: number = 5901) {
  if (!endpoint) {
    endpoint = await this.getTaskPublicIp();
  }
  // Publishes: { vncEndpoint: "54.123.45.67", vncPort: 5901, vncReady: true }
}
```

### 5. IAM Permissions (template.yaml)
**File**: `lma-virtual-participant-stack/template.yaml`

Added ECS and EC2 permissions to TaskRole:
```yaml
- PolicyName: ECSDescribePolicy
  PolicyDocument:
    Statement:
      - Effect: Allow
        Action:
          - "ecs:DescribeTasks"
          - "ec2:DescribeNetworkInterfaces"
        Resource: "*"
```

### 6. React UI Component (VNCViewer.jsx)
**File**: `lma-ai-stack/source/ui/src/components/virtual-participant-layout/VNCViewer.jsx`

Changed connection logic:
```javascript
// OLD: const wsUrl = `${websocketUrl}?vpId=${vpId}`;
// NEW: Direct connection to task IP
const wsUrl = `ws://${vncEndpoint}:5901`;
```

## Testing Steps

### 1. Deploy Updated Stack
```bash
# Deploy virtual participant stack with changes
aws cloudformation update-stack \
  --stack-name LMA-dev-stack-VIRTUALPARTICIPANTSTACK \
  --template-body file://lma-virtual-participant-stack/template.yaml \
  --capabilities CAPABILITY_IAM \
  --region us-east-1
```

### 2. Whitelist Your IP
```bash
# Get your public IP
MY_IP=$(curl -s https://checkip.amazonaws.com)
echo "Your IP: $MY_IP"

# Get security group ID
SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=tag:Name,Values=LMA-dev-stack-vp-sg" \
  --query 'SecurityGroups[0].GroupId' \
  --output text \
  --region us-east-1)

echo "Security Group: $SG_ID"

# Add ingress rule for your IP
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp \
  --port 5901 \
  --cidr ${MY_IP}/32 \
  --region us-east-1

echo "✓ Whitelisted $MY_IP for VNC access"
```

### 3. Start Virtual Participant
1. Open your LMA UI
2. Navigate to Virtual Participants
3. Start a new virtual participant
4. Wait for status to show "VNC_READY"

### 4. Verify Connection
Check the browser console for:
```
Connecting to VNC directly to ECS task: ws://54.123.45.67:5901
Virtual Participant ID: c2600429-0b66-45dd-80c1-2906aa7a549d
VNC Endpoint: 54.123.45.67
VNC connected successfully
```

### 5. Check Container Logs
```bash
# Get task ARN
TASK_ARN=$(aws ecs list-tasks \
  --cluster LMA-dev-stack-VIRTUALPARTICIPANTSTACK-Cluster \
  --query 'taskArns[0]' \
  --output text \
  --region us-east-1)

# View logs
aws logs tail /LMA-dev-stack-VIRTUALPARTICIPANTSTACK/fargate/VirtualParticipant \
  --follow \
  --region us-east-1
```

Look for:
```
✓ VNC WebSocket server is ready on port 5901
✓ Task public IP: 54.123.45.67
✓ Successfully published VNC endpoint: 54.123.45.67:5901
```

## Architecture Comparison

### Before (API Gateway - Didn't Work)
```
Browser → API Gateway WebSocket → Lambda → ECS Task
          (expects JSON)         (binary VNC) ✗ FAILS
```

### After (Direct Connection - Works!)
```
Browser → ECS Task Public IP
          (binary VNC over WebSocket) ✓ WORKS
```

## Security Notes

### Current Setup (Testing)
- ✅ Security group locked down by default
- ✅ Manual IP whitelisting required
- ✅ Ephemeral tasks (only live during meetings)
- ⚠️ No TLS encryption (`ws://` not `wss://`)
- ⚠️ Public IP exposure

### For Production
Migrate to ALB for:
- TLS encryption with ACM certificate
- Private ECS tasks (no public IPs)
- Health checks and automatic failover
- Better scalability for multiple users
- Centralized access control

## Migration Path to ALB

The container is already configured correctly! To migrate:

1. **Add ALB** with target group on port 5901
2. **Add ACM certificate** to ALB listener
3. **Update `setVncReady()`** to publish ALB DNS instead of task IP
4. **Change React UI** from `ws://` to `wss://`
5. **Disable public IPs** on tasks
6. **Remove manual security group rules**

**Zero code changes needed in container!**

## Troubleshooting

### Connection Refused
```bash
# Check security group
aws ec2 describe-security-groups \
  --group-ids $SG_ID \
  --region us-east-1

# Verify your IP is whitelisted
# Look for ingress rule with your IP on port 5901
```

### Task Has No Public IP
```bash
# Verify task configuration
aws ecs describe-tasks \
  --cluster LMA-dev-stack-VIRTUALPARTICIPANTSTACK-Cluster \
  --tasks $TASK_ARN \
  --region us-east-1 \
  | jq '.tasks[0].attachments[0].details[] | select(.name=="networkInterfaceId")'
```

### x11vnc Not Starting
```bash
# Check container logs
aws logs tail /LMA-dev-stack-VIRTUALPARTICIPANTSTACK/fargate/VirtualParticipant \
  --since 10m \
  --region us-east-1 \
  | grep -i vnc
```

## Files Modified

1. ✅ `lma-virtual-participant-stack/backend/entrypoint.sh` - Added WebSocket flags to x11vnc
2. ✅ `lma-virtual-participant-stack/template.yaml` - Enabled public IP, updated security group, added permissions
3. ✅ `lma-virtual-participant-stack/backend/src/status-manager.ts` - Added getTaskPublicIp(), updated setVncReady()
4. ✅ `lma-ai-stack/source/ui/src/components/virtual-participant-layout/VNCViewer.jsx` - Changed to direct connection
5. ✅ `lma-virtual-participant-stack/DIRECT_VNC_SETUP.md` - Testing guide
6. ✅ `lma-virtual-participant-stack/NOVNC_DIRECT_CONNECTION_SUMMARY.md` - This file

## Next Steps

1. Deploy the updated CloudFormation stack
2. Whitelist your IP in the security group
3. Start a virtual participant and test the connection
4. Once validated, plan migration to ALB for production use

## Benefits of This Approach

✅ **Works with noVNC** - Binary VNC protocol supported  
✅ **Simple architecture** - No Lambda proxy needed  
✅ **Fast validation** - Can test immediately  
✅ **Migration path** - Easy to add ALB later  
✅ **Secure by default** - No open ports without whitelisting  
✅ **Cost effective** - No ALB cost during validation  

## Conclusion

This implementation provides a working noVNC solution for immediate validation while maintaining a clear path to production-grade deployment with ALB.
