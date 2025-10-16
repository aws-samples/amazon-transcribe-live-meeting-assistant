# noVNC Deployment Guide for LMA Virtual Participant

## Overview
This guide provides instructions for deploying the noVNC integration for real-time browser-based viewing and interaction with LMA Virtual Participants.

## Architecture

The implementation uses **API Gateway WebSocket + Network Load Balancer** with:
- ✅ **AWS-managed SSL certificate** (no certificate management required!)
- ✅ **No manual configuration** - Everything automated via CloudFormation
- ✅ **Private networking** - NLB and ECS tasks in private subnets
- ✅ **AppSync signaling** - Real-time VNC endpoint updates

## Prerequisites

**None!** All required resources are created automatically or already exist from your LMA deployment:
- Cognito User Pool (from LMA AI Stack)
- VPC and subnets (from LMA VPC Stack or existing VPC)
- API Gateway WebSocket API (created automatically with AWS-managed SSL)

## Deployment Steps

```bash
# Deploy or update your main LMA stack
aws cloudformation deploy \
  --template-file lma-main.yaml \
  --stack-name your-lma-stack-name \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides \
    AdminEmail=admin@example.com \
    # ... your other existing parameters
```

That's it! The stack will automatically:
1. Create API Gateway WebSocket API (with AWS-managed SSL certificate)
2. Create Network Load Balancer (internal, in private subnets)
3. Create VPC Link (connects API Gateway to NLB)
4. Store WebSocket URL in LMA Settings
5. Pass WebSocket URL to AI Stack
6. Build UI with WebSocket URL embedded
7. Deploy everything

### Step 2: Verify Deployment

```bash
# Check main stack status
aws cloudformation describe-stacks \
  --stack-name your-lma-stack-name \
  --query 'Stacks[0].StackStatus'

# Get the VNC WebSocket URL (for reference)
aws cloudformation describe-stacks \
  --stack-name your-lma-stack-name-VIRTUALPARTICIPANTSTACK-xxx \
  --query 'Stacks[0].Outputs[?OutputKey==`VNCWebSocketURL`].OutputValue' \
  --output text
```

The WebSocket URL will be something like:
`wss://abc123xyz.execute-api.us-east-1.amazonaws.com/prod`

### Step 3: Test the Implementation

1. Log into LMA UI
2. Start a virtual participant
3. Navigate to Virtual Participant Details page
4. Wait 10-30 seconds for VNC server to start
5. VNC viewer appears automatically
6. Click inside viewer to interact
7. Test CAPTCHA handling or other interactions

## Verification Steps

### 1. Verify Stack Deployment

```bash
# Check stack status
aws cloudformation describe-stacks \
  --stack-name lma-virtual-participant-stack \
  --query 'Stacks[0].StackStatus'

# Verify ALB is healthy
aws elbv2 describe-target-health \
  --target-group-arn <target-group-arn>
```

### 2. Test VNC Server in Container

Start a virtual participant and check the logs:

```bash
# Get the task ARN
TASK_ARN=$(aws ecs list-tasks \
  --cluster <cluster-name> \
  --query 'taskArns[0]' \
  --output text)

# View container logs
aws logs tail /lma-virtual-participant-stack/fargate/VirtualParticipant --follow
```

Look for these log messages:
- ✓ VNC WebSocket server is ready on port 5901
- ✓ VNC ready signal created
- ✓ VNC endpoint published: <ip>:5901

### 3. Test ALB Connectivity

```bash
# Test ALB health
curl -I https://<alb-dns-name>/

# Should redirect to Cognito login if not authenticated
```

### 4. Test End-to-End Flow

1. Log into LMA UI
2. Start a virtual participant
3. Navigate to Virtual Participant Details page
4. Wait for "Preparing live view..." message
5. VNC viewer should appear automatically when ready
6. Click inside viewer to interact
7. Test mouse and keyboard control

## Troubleshooting

### Issue: ALB Health Checks Failing

**Symptoms**: Target group shows unhealthy targets

**Solutions**:
```bash
# Check security group rules
aws ec2 describe-security-groups --group-ids <sg-id>

# Verify port 5901 is open from ALB to ECS tasks
# Check VNC server logs in container
```

### Issue: VNC Viewer Not Appearing

**Symptoms**: UI shows "Preparing live view..." indefinitely

**Solutions**:
1. Check if `vncReady` is being set in DynamoDB
2. Verify GraphQL subscription is receiving updates
3. Check browser console for errors
4. Verify ALB DNS is correctly configured in UI

### Issue: Connection Timeout

**Symptoms**: VNC viewer shows "Failed to connect"

**Solutions**:
1. Verify Cognito authentication is working
2. Check ALB listener rules
3. Verify target group has healthy targets
4. Check browser network tab for WebSocket errors

### Issue: Black Screen in VNC

**Symptoms**: Connected but screen is black

**Solutions**:
1. Check if Xvfb is running: `ps aux | grep Xvfb`
2. Verify DISPLAY=:99 environment variable
3. Check if Chromium launched successfully
4. Review container logs for errors

### Issue: High Latency

**Symptoms**: Slow response to mouse/keyboard input

**Solutions**:
1. Check network path (ALB → ECS)
2. Verify x11vnc is using `-speeds lan`
3. Consider reducing resolution if needed
4. Check if ALB is in same region as ECS tasks

## Monitoring

### CloudWatch Metrics to Monitor

```bash
# ALB metrics
- TargetResponseTime
- HealthyHostCount
- UnHealthyHostCount
- RequestCount

# ECS metrics
- CPUUtilization
- MemoryUtilization

# Custom metrics (from logs)
- VNC connection count
- VNC connection duration
- VNC errors
```

### CloudWatch Logs Insights Queries

**VNC Connection Events**:
```
fields @timestamp, @message
| filter @message like /VNC_AUDIT/
| parse @message "VNC_AUDIT: *" as audit_data
| display @timestamp, audit_data
```

**VNC Errors**:
```
fields @timestamp, @message
| filter @message like /VNC/ and @message like /error|failed|ERROR|FAILED/
| sort @timestamp desc
```

## Cost Considerations

### Additional Costs from noVNC Implementation

1. **Application Load Balancer**: ~$16-25/month base + data transfer
2. **Increased ECS Task Memory**: 4GB (was 2GB) - ~$0.04/hour per task
3. **Data Transfer**: ~1-5 Mbps per active VNC session
4. **CloudWatch Logs**: Minimal increase

**Estimated Additional Cost**: $20-50/month for typical usage

## Security Best Practices

1. ✅ **Restrict ALB Access** (if possible):
   ```yaml
   SecurityGroupIngress:
     - IpProtocol: tcp
       FromPort: 443
       ToPort: 443
       CidrIp: 10.0.0.0/8  # Your corporate network
   ```

2. ✅ **Enable ALB Access Logs**:
   ```yaml
   LoadBalancerAttributes:
     - Key: access_logs.s3.enabled
       Value: 'true'
     - Key: access_logs.s3.bucket
       Value: your-log-bucket
   ```

3. ✅ **Add WAF** (optional):
   ```bash
   aws wafv2 associate-web-acl \
     --web-acl-arn <waf-acl-arn> \
     --resource-arn <alb-arn>
   ```

4. ✅ **Monitor Access**:
   - Set up CloudWatch alarms for unusual activity
   - Review VNC audit logs regularly
   - Monitor failed authentication attempts

## Rollback Procedure

If you need to rollback the changes:

```bash
# Revert to previous stack version
aws cloudformation update-stack \
  --stack-name lma-virtual-participant-stack \
  --use-previous-template \
  --parameters <previous-parameters>

# Or rollback to last stable version
aws cloudformation cancel-update-stack \
  --stack-name lma-virtual-participant-stack
```

## Support and Documentation

- **Implementation Details**: See `NOVNC_IMPLEMENTATION.md`
- **Architecture Diagram**: See documentation for flow diagrams
- **noVNC Documentation**: https://github.com/novnc/noVNC
- **AWS ALB WebSocket**: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-listeners.html

## Next Steps After Deployment

1. **Test with a real meeting** - Start a VP and verify VNC connection
2. **Document for users** - Create user guide for CAPTCHA handling
3. **Set up monitoring** - Configure CloudWatch alarms
4. **Performance tuning** - Adjust resolution/quality if needed
5. **Security review** - Ensure all security measures are in place
