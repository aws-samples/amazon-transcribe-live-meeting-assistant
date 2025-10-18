# CloudFront VNC Implementation Plan

## Overview
Add VNC ALB as an origin to the existing CloudFront distribution to enable `wss://` connections without requiring a custom domain.

## Changes Needed

### 1. AI Stack Template (`lma-ai-stack/deployment/lma-ai-stack.yaml`)

Add VNC origin and cache behavior to `WebAppCloudFrontDistribution`:

```yaml
Origins:
  - Id: webapp-s3-bucket
    DomainName: !GetAtt WebAppBucket.RegionalDomainName
    S3OriginConfig:
      OriginAccessIdentity: !Sub "origin-access-identity/cloudfront/${CloudFrontOriginAccessIdentity}"
  
  # NEW: VNC ALB Origin
  - Id: vnc-alb
    DomainName: !Ref VNCALBDNSName
    CustomOriginConfig:
      HTTPPort: 80
      HTTPSPort: 443
      OriginProtocolPolicy: http-only
      OriginSSLProtocols:
        - TLSv1.2

CacheBehaviors:
  # NEW: VNC WebSocket Behavior
  - PathPattern: /vnc/*
    TargetOriginId: vnc-alb
    ViewerProtocolPolicy: https-only
    AllowedMethods:
      - GET
      - HEAD
      - OPTIONS
      - PUT
      - POST
      - PATCH
      - DELETE
    CachedMethods:
      - GET
      - HEAD
    ForwardedValues:
      QueryString: true
      Headers:
        - Upgrade
        - Connection
        - Sec-WebSocket-Key
        - Sec-WebSocket-Version
        - Sec-WebSocket-Protocol
        - Sec-WebSocket-Extensions
      Cookies:
        Forward: none
    Compress: false
    DefaultTTL: 0
    MinTTL: 0
    MaxTTL: 0
```

### 2. Main Stack (`lma-main.yaml`)

Pass VNC ALB DNS to AI stack:

```yaml
AISTACK:
  Parameters:
    # ... existing parameters ...
    VNCALBDNSName: !GetAtt VIRTUALPARTICIPANTSTACK.Outputs.VNCALBDNS
```

### 3. Virtual Participant Stack

Update VNC WebSocket URL to use CloudFront:

```yaml
UpdateVNCWebSocketURL:
  Properties:
    LCASettingsKeyValuePairs:
      VNCWebSocketURL: !Sub "wss://${CloudFrontDomainName}/vnc"
```

## IP Restriction Options

### Option A: ALB Security Group (Simplest)
Add parameter to Virtual Participant stack:

```yaml
Parameters:
  AllowedVNCClientIP:
    Type: String
    Default: "0.0.0.0/0"
    Description: IP address or CIDR allowed to access VNC (e.g., 203.0.113.45/32)

Resources:
  ALBSecurityGroup:
    Properties:
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 80
          ToPort: 80
          CidrIp: !Ref AllowedVNCClientIP
```

### Option B: Lambda@Edge (Production)
Add Lambda function to validate Cognito tokens for `/vnc/*` requests.

## Multi-User Routing

Current issue: All users connect to same CloudFront URL → ALB round-robins to random tasks.

**Solution:** Path-based routing with vpId:
- User A: `wss://cloudfront-url/vnc/abc123`
- User B: `wss://cloudfront-url/vnc/def456`
- ALB routes based on path to correct task

Requires:
1. Dynamic target group registration (Lambda)
2. ALB listener rules per vpId
3. Task publishes its private IP + vpId

## Deployment Order

1. Deploy Virtual Participant stack (creates ALB)
2. Deploy AI stack (adds VNC origin to CloudFront)
3. Deploy main stack (connects everything)

## Testing

1. Start virtual participant
2. AppSync publishes `vncReady: true`
3. UI connects to `wss://cloudfront-url/vnc`
4. CloudFront forwards to ALB
5. ALB routes to ECS task
6. User sees live virtual participant!

## Future Enhancements

- [ ] Add Lambda@Edge for Cognito authentication
- [ ] Implement path-based routing for multi-user
- [ ] Add proper HTTPS listener on ALB
- [ ] Add CloudWatch metrics and alarms
