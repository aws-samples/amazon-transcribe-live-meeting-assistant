# IAM Permission Fix for ALB Target Health Checks

## Issue
The ECS task was failing with the following error:
```
AccessDenied: User: arn:aws:sts::190164553733:assumed-role/LMA-dev-stack-VIRTUALPARTICIPANTSTACK-EFWN-TaskRole-tQFfrQYKgWXe/42724120a99a4003979ec2b781ffa650 is not authorized to perform: elasticloadbalancing:DescribeTargetHealth because no identity-based policy allows the elasticloadbalancing:DescribeTargetHealth action
```

## Root Cause
The TaskRole IAM policy included `RegisterTargets` and `DeregisterTargets` permissions, but was missing the `DescribeTargetHealth` permission that the code uses to verify the target is healthy after registration.

## Solution
Updated the `ALBTargetRegistrationPolicy` in `template.yaml` to include two separate statements:

1. **Target Registration/Deregistration** - Scoped to specific target group:
   ```yaml
   - Effect: Allow
     Action:
       - "elasticloadbalancing:RegisterTargets"
       - "elasticloadbalancing:DeregisterTargets"
     Resource:
       - !Ref VNCTargetGroupArn
   ```

2. **Health Check Queries** - Requires wildcard resource:
   ```yaml
   - Effect: Allow
     Action:
       - "elasticloadbalancing:DescribeTargetHealth"
     Resource:
       - "*"
   ```

## Why Wildcard Resource?
The `DescribeTargetHealth` action requires a wildcard resource (`*`) because:
- It can query health status across multiple target groups
- AWS IAM doesn't support resource-level permissions for this specific action
- This is a read-only operation that doesn't modify resources

## Testing
After deploying this fix:
1. The ECS task should successfully register with the ALB target group
2. The health check polling should work without AccessDenied errors
3. The task should wait for the target to become healthy before proceeding
4. The VNC_READY signal should be sent once the target is healthy

## Files Modified
- `lma-virtual-participant-stack/template.yaml` - Updated TaskRole IAM policy
