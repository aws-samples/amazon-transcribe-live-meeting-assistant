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

1. **CloudFormationServiceRole** — An IAM role that only `cloudformation.amazonaws.com` can assume. It has three inline policies covering all AWS services required by LMA.
2. **PassRolePolicy** — A managed policy that grants `iam:PassRole` for the service role. Attach this to users or roles that need to deploy LMA.

It takes one parameter, `PermissionsBoundaryArn`, described under
[Permissions boundary](#permissions-boundary-recommended) below.

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

## Permissions boundary (recommended)

The service role creates the IAM roles that the LMA stacks need (Lambda execution roles, ECS task roles, and so on). The recommended configuration is to supply an [IAM permissions boundary](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html) so that every role created through the service role is capped by that boundary, which keeps the roles the deployment produces inside limits your security team sets once, independently of the service role's own policies.

Supply it through the `PermissionsBoundaryArn` parameter. When the parameter is set, the service role's `iam:CreateRole`, `iam:PutRolePolicy` and `iam:AttachRolePolicy` grants carry an `iam:PermissionsBoundary` condition, so a role can only be created — and only have policies written to it — while it carries that exact boundary policy. When the parameter is left empty (the default), the template behaves as it did before and role creation is not tied to a boundary.

### Setting it up

1. **Create the boundary policy** (once, by an administrator). It must allow everything the LMA roles legitimately do. A practical starting point is to copy the action lists from the three inline policies in `LMA-Cloudformation-Service-Role.yaml` and narrow them to your account's resources and regions.

   ```bash
   aws iam create-policy \
     --policy-name LMA-PermissionsBoundary \
     --policy-document file://lma-permissions-boundary.json
   ```

2. **Deploy the service role with the boundary ARN:**

   ```bash
   BOUNDARY_ARN=arn:aws:iam::123456789012:policy/LMA-PermissionsBoundary

   cd iam-roles/cloudformation-management/
   aws cloudformation deploy \
     --template-file LMA-Cloudformation-Service-Role.yaml \
     --stack-name LMA-CFServiceRole \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides PermissionsBoundaryArn=$BOUNDARY_ARN \
     --region <your-region>
   ```

3. **Pass the same ARN to the LMA stack.** `lma-main.yaml` has its own `PermissionsBoundaryArn` parameter (default `""`), and that is what makes each LMA stack *attach* the boundary to the roles it defines. The two parameters are two halves of one setting: the service-role parameter requires the boundary, the LMA stack parameter supplies it. **Set both to the same ARN, or neither.** If the service role requires a boundary that the LMA stack does not attach, role creation is refused and the stack operation fails.

   ```bash
   # Read the required boundary back from the service role stack
   BOUNDARY_ARN=$(aws cloudformation describe-stacks \
     --stack-name LMA-CFServiceRole \
     --query 'Stacks[0].Outputs[?OutputKey==`RequiredPermissionsBoundaryArn`].OutputValue' \
     --output text)

   lma deploy --stack-name MyLMA --admin-email user@example.com \
     --role-arn $ROLE_ARN \
     -p PermissionsBoundaryArn=$BOUNDARY_ARN --wait
   ```

### Coverage note

Not every IAM role across every LMA nested stack attaches the boundary today — the VPC stack's flow-logs role, for example, does not take the parameter. Try this on a test stack before enabling it on an existing deployment. If a stack operation stops with an authorization failure on `iam:CreateRole`, either add the `PermissionsBoundary` property to that role's definition or redeploy the service role with `PermissionsBoundaryArn=""` to return to the unconstrained behaviour.

Updating a deployment *to* a boundary also rewrites existing roles, so the service role is granted `iam:PutRolePermissionsBoundary` when — and only when — a boundary is configured. The matching delete action is not granted, so the boundary can be applied through this role but not removed through it.

## Deploying the Service Role

### Prerequisites

- AWS Administrator access (one-time setup)
- AWS CLI configured with appropriate credentials
- Optionally, a permissions boundary managed policy — see [Permissions boundary](#permissions-boundary-recommended)

### Via CLI

```bash
cd iam-roles/cloudformation-management/

aws cloudformation deploy \
  --template-file LMA-Cloudformation-Service-Role.yaml \
  --stack-name LMA-CFServiceRole \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides PermissionsBoundaryArn=<boundary-policy-arn> \
  --region <your-region>
```

Omit `--parameter-overrides` to deploy without a boundary.

### Via Console

1. Open the AWS CloudFormation console
2. Click **Create stack** → **With new resources (standard)**
3. Select **Upload a template file** and choose `LMA-Cloudformation-Service-Role.yaml`
4. Set **Stack name** to `LMA-CFServiceRole` (or your preferred name)
5. Set **PermissionsBoundaryArn** to your boundary policy ARN (leave blank to deploy without one)
6. Click through **Next**, acknowledge IAM capabilities, and **Submit**
7. Wait for `CREATE_COMPLETE`
8. Copy the **ServiceRoleArn** and **RequiredPermissionsBoundaryArn** values from the **Outputs** tab

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
| **Storage & Data** | S3, S3 Vectors, DynamoDB, Kinesis |
| **API & Application** | AppSync, CloudFront, Elastic Load Balancing |
| **Security & Identity** | Cognito, KMS, Secrets Manager |
| **Messaging & Events** | SNS, SES, EventBridge, EventBridge Scheduler |
| **Monitoring** | CloudWatch Logs, X-Ray |
| **Networking** | EC2/VPC, Auto Scaling |
| **Marketplace** | AWS Marketplace |

### Security Details

- **Trust policy** restricts role assumption to `cloudformation.amazonaws.com` only
- **Permissions boundary** — when `PermissionsBoundaryArn` is supplied, `iam:CreateRole`, `iam:PutRolePolicy` and `iam:AttachRolePolicy` are conditioned on `iam:PermissionsBoundary`, so the roles created for LMA stay within that boundary policy. This is the recommended configuration; see [Permissions boundary](#permissions-boundary-recommended)
- **PassRole** lets CloudFormation hand the roles it creates to the services that consume them (Lambda, ECS, CodeBuild, AppSync, Step Functions, Bedrock, and the other services listed above)
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
| **LMA deployment fails on `iam:CreateRole` after setting a boundary** | The LMA stack must attach the same boundary it is required to carry. Set the LMA stack's `PermissionsBoundaryArn` to the value of the service role stack's `RequiredPermissionsBoundaryArn` output, or redeploy the service role with `PermissionsBoundaryArn=""` |
| **Boundary policy too narrow** | The boundary caps what the LMA roles can do, so anything it omits is unavailable to them at runtime. Widen the boundary policy rather than removing it |

## Cleanup

```bash
aws cloudformation delete-stack --stack-name LMA-CFServiceRole
```

This removes both the service role and the PassRole policy.
