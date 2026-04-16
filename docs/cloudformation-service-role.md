---
title: "CloudFormation Service Role"
---

# CloudFormation Service Role for LMA Deployment

This guide explains how to create a dedicated IAM CloudFormation service role for deploying, managing, and modifying Live Meeting Assistant (LMA) stacks — without requiring administrator access for every deployment.

The CloudFormation template is located at [`iam-roles/cloudformation-management/LMA-Cloudformation-Service-Role.yaml`](../iam-roles/cloudformation-management/LMA-Cloudformation-Service-Role.yaml).

## Why Use a CloudFormation Service Role?

By default, CloudFormation operations use the caller's IAM permissions. This means anyone deploying LMA needs broad AWS access. A **CloudFormation service role** decouples deployment permissions from user permissions:

- **Administrators** deploy the service role once with their elevated privileges
- **Developer/DevOps users** can then deploy and manage LMA stacks by passing this role to CloudFormation — without needing admin permissions themselves
- **Operational teams** can maintain the solution without ongoing administrator access
- **Security teams** can audit a single role rather than individual user policies

## How It Works

The template creates two resources:

1. **CloudFormationServiceRole** — An IAM role that only `cloudformation.amazonaws.com` can assume. It has four inline policies covering all ~30 AWS services required by LMA.
2. **PassRolePolicy** — A managed policy that grants `iam:PassRole` for the service role. Attach this to users or roles that need to deploy LMA.

```
┌─────────────────┐     iam:PassRole     ┌───────────────────┐     sts:AssumeRole     ┌──────────────┐
│   IAM User or   │ ──────────────────► │  CloudFormation   │ ──────────────────────► │  LMA Service │
│   Developer     │                      │  Service          │                         │  Role        │
└─────────────────┘                      └───────────────────┘                         └──────┬───────┘
                                                                                              │
                                                                                    Creates/Updates/Deletes
                                                                                              │
                                                                                              ▼
                                                                                     ┌──────────────┐
                                                                                     │  LMA Stack   │
                                                                                     │  Resources   │
                                                                                     └──────────────┘
```

## Deploying the Service Role

### Prerequisites

- AWS Administrator access (one-time setup)
- AWS CLI configured with appropriate credentials

### Via CLI

```bash
cd iam-roles/cloudformation-management/

aws cloudformation deploy \
  --template-file LMA-Cloudformation-Service-Role.yaml \
  --stack-name LMA-CFServiceRole \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <your-region>
```

### Via Console

1. Open the AWS CloudFormation console
2. Click **Create stack** → **With new resources (standard)**
3. Select **Upload a template file** and choose `LMA-Cloudformation-Service-Role.yaml`
4. Set **Stack name** to `LMA-CFServiceRole` (or your preferred name)
5. Click through **Next**, acknowledge IAM capabilities, and **Submit**
6. Wait for `CREATE_COMPLETE`
7. Copy the **ServiceRoleArn** from the **Outputs** tab

## Assigning the PassRole Policy to Users

After deploying the service role stack, attach the `PassRolePolicy` to users or roles who need to deploy LMA:

```bash
# Get the PassRole policy ARN from stack outputs
POLICY_ARN=$(aws cloudformation describe-stacks \
  --stack-name LMA-CFServiceRole \
  --query 'Stacks[0].Outputs[?OutputKey==`PassRolePolicyArn`].OutputValue' \
  --output text)

# Attach to a user
aws iam attach-user-policy --user-name <username> --policy-arn $POLICY_ARN

# Or attach to a role
aws iam attach-role-policy --role-name <role-name> --policy-arn $POLICY_ARN
```

## Using the Service Role to Deploy LMA

### Via LMA CLI (recommended)

The `lma-cli deploy` command supports `--role-arn`:

```bash
# Get the service role ARN
ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name LMA-CFServiceRole \
  --query 'Stacks[0].Outputs[?OutputKey==`ServiceRoleArn`].OutputValue' \
  --output text)

# Deploy LMA
lma-cli deploy --stack-name MyLMA --admin-email user@example.com --role-arn $ROLE_ARN --wait
```

### Via AWS CLI

```bash
ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name LMA-CFServiceRole \
  --query 'Stacks[0].Outputs[?OutputKey==`ServiceRoleArn`].OutputValue' \
  --output text)

aws cloudformation create-stack \
  --stack-name LMA \
  --template-url <lma-template-url> \
  --role-arn $ROLE_ARN \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --parameters ...
```

### Via Console

1. Navigate to the CloudFormation console
2. Click **Create stack** → choose the LMA template
3. In the **Configure stack options** step, under **Permissions**, select the service role
4. Complete the deployment as normal

## AWS Service Permissions

The role provides access to the following AWS services required by LMA:

| Category | Services |
|----------|----------|
| **Core Infrastructure** | CloudFormation, IAM, Serverless Application Repository |
| **Compute & Serverless** | Lambda, Step Functions, CodeBuild, ECS, ECR |
| **AI/ML Services** | Bedrock, Bedrock AgentCore, Transcribe, Translate, Comprehend |
| **Storage & Data** | S3, S3 Vectors, DynamoDB, Kinesis, OpenSearch Serverless |
| **API & Application** | AppSync, CloudFront, Elastic Load Balancing |
| **Security & Identity** | Cognito, KMS, Secrets Manager, SSO/Identity Center |
| **Messaging & Events** | SNS, SES, EventBridge, EventBridge Scheduler |
| **Monitoring** | CloudWatch Logs, X-Ray |
| **Networking** | EC2/VPC, Auto Scaling |
| **Optional** | Lex, Q Business, AWS Marketplace |

### Security Details

- **Trust policy** restricts role assumption to `cloudformation.amazonaws.com` only
- **PassRole** is constrained by `iam:PassedToService` condition to specific AWS services (Lambda, ECS, CodeBuild, AppSync, Step Functions, Bedrock, etc.)
- **Service-linked role creation** is limited to the ECS service
- All CloudFormation operations using this role are logged in **CloudTrail**
- Organizations may further restrict permissions based on their specific compliance requirements

## Troubleshooting

| Issue | Resolution |
|-------|------------|
| **Access Denied when deploying LMA** | Verify the user has the `PassRolePolicy` attached |
| **Stack creation fails with capability error** | Include `CAPABILITY_NAMED_IAM` when deploying the service role template |
| **Missing permissions during LMA deployment** | This role covers all known LMA services. If new services are added, update the template and redeploy |
| **Role name conflicts** | The role name includes the stack name — use a unique stack name |

## Cleanup

```bash
aws cloudformation delete-stack --stack-name LMA-CFServiceRole
```

This removes both the service role and the PassRole policy.
