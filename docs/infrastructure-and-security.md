# Infrastructure & Security

## Table of Contents

- [Overview](#overview)
- [Architecture Diagram](#architecture-diagram)
- [Processing Flow](#processing-flow)
- [Nested Stack Structure](#nested-stack-structure)
- [Authentication](#authentication)
- [Encryption](#encryption)
- [Networking](#networking)
- [CloudFront CDN](#cloudfront-cdn)
- [IAM](#iam)
- [Observability](#observability)
- [Data Retention](#data-retention)
- [Related Documentation](#related-documentation)

## Overview

LMA is deployed as a set of 11 nested CloudFormation stacks providing a fully serverless, event-driven architecture. This document describes the infrastructure components, security controls, and operational characteristics of the system.

## Architecture Diagram

![LMA Architecture](../images/lma-architecture.png)

## Processing Flow

1. **User starts meeting** via the Stream Audio tab or by launching a Virtual Participant.
2. A **secure WebSocket connection** is established to the Fargate-based WebSocket server.
3. **Two-channel audio** is streamed to Amazon Transcribe.
4. **Transcription results** are written to a Kinesis Data Stream in real time.
5. The **Call Event Processor Lambda** consumes from Kinesis, processes transcript segments, and integrates with the Strands agent for meeting assistance.
6. **Results are persisted** to DynamoDB via AppSync mutations.
7. **Real-time updates** are pushed to the UI via AppSync GraphQL subscriptions.
8. **End-to-end latency** is typically a few seconds.

## Nested Stack Structure

| Stack | Purpose |
|-------|---------|
| lma-ai-stack | Core stack: Lambda functions, AppSync API, DynamoDB, Cognito, CloudFront UI |
| lma-websocket-transcriber-stack | WebSocket server on ECS Fargate with Application Load Balancer |
| lma-virtual-participant-stack | VP ECS tasks and Step Functions scheduler |
| lma-vpc-stack | VPC, public/private subnets (2 AZs), NAT gateway, security groups |
| lma-cognito-stack | Cognito User Pool and Identity Pool |
| lma-meetingassist-setup-stack | Strands agent configuration |
| lma-bedrockkb-stack | Bedrock Knowledge Base setup |
| lma-llm-template-setup-stack | LLM prompt templates (DynamoDB) |
| lma-chat-button-config-stack | Chat button configuration (DynamoDB) |
| lma-nova-sonic-config-stack | Nova Sonic voice assistant configuration |

## Authentication

LMA uses **Amazon Cognito User Pool** for authentication with the following features:

- Optional multi-factor authentication (MFA)
- JWT token verification with signature validation
- Admin group for elevated access privileges
- Self-registration with configurable email domain restrictions

## Encryption

### At Rest

- **Customer-managed KMS key** encrypts DynamoDB tables, S3 buckets, and CloudWatch Log groups
- All sensitive data is encrypted using AES-256 via KMS

### In Transit

- **HTTPS/TLS** for all connections including WebSocket, AppSync, and CloudFront
- All API communications are encrypted end-to-end

## Networking

- **VPC** with public and private subnets across 2 Availability Zones for high availability
- **NAT gateway** for outbound internet access from private subnets
- **Application Load Balancer** for the WebSocket server endpoint
- **Security groups** for network-level access control between components
- **Optional**: Use an existing VPC and subnets instead of the auto-created network infrastructure

## CloudFront CDN

- Configurable **price class**: PriceClass_100 (North America and Europe), PriceClass_200 (adds Asia, Africa, Middle East), or PriceClass_All (all edge locations)
- Optional **geographic restrictions** via ISO 3166-1 alpha-2 country codes to limit access by region

## IAM

- **Least-privilege policies** for all IAM roles created by the stack
- Optional **permissions boundary** via the `InstallationPermissionsBoundaryArn` parameter, applied to all roles created during installation

## Observability

- **AWS X-Ray tracing** for distributed request tracing across Lambda functions and API calls
- **CloudWatch Logs** with configurable retention (1 to 3653 days)
- **CloudWatch metrics** for Lambda, ECS, and AppSync services

## Data Retention

| Data Type | Retention | Configuration |
|-----------|-----------|---------------|
| Meeting records | Configurable, default 90 days | DynamoDB TTL via `MeetingRecordExpirationInDays` parameter |
| Audio recordings | Configurable | S3 lifecycle policy |
| Transcript KB data | Configurable | S3 lifecycle policy |
| CloudWatch logs | Configurable | `CloudWatchLogsExpirationInDays` parameter |

The stack parameter **EnableDataRetentionOnDelete** controls whether DynamoDB tables, S3 buckets, and KMS keys are retained when the stack is deleted. When enabled, these resources persist after stack deletion to prevent data loss.

## Related Documentation

- [CloudFormation Parameters Reference](cloudformation-parameters.md)
- [Troubleshooting](troubleshooting.md)
