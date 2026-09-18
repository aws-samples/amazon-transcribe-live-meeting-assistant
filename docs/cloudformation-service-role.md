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

It takes one parameter, `PermissionsBoundaryArn`, which should be left at its default
empty value — see [Permissions boundary](#permissions-boundary-not-yet-supported-end-to-end)
below.

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

## Permissions boundary (not yet supported end to end)

The service role creates the IAM roles that the LMA stacks need (Lambda execution roles, ECS task roles, and so on). The template accepts an [IAM permissions boundary](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html) through its `PermissionsBoundaryArn` parameter so that role creation through the service role can be required to carry that boundary.

⚠️ **This is not a supported configuration yet.** Setting `PermissionsBoundaryArn` to a non-empty value will stop an LMA deployment, because LMA does not yet attach a boundary to every role it creates — see [Current coverage](#current-coverage) for the measured numbers. The parameter is here so the boundary work can be completed and tested incrementally. Leave it at its default empty value for a normal deployment.

When the parameter is set, the service role's `iam:CreateRole`, `iam:PutRolePolicy` and `iam:AttachRolePolicy` grants carry an `iam:PermissionsBoundary` condition, so a role can only be created — and only have policies written to it — while it carries that exact boundary policy. When the parameter is left empty (the default), a CloudFormation `Condition` selects an alternative statement with no condition and the template behaves exactly as it did before.

### Current coverage

Counted by static inspection of every CloudFormation template in the repository at this revision:

| | Count |
|---|---|
| `AWS::IAM::Role` resources declared across the LMA templates | 102 |
| …of which set a `PermissionsBoundary` property | 54 |
| …in `lma-ai-stack` alone | 51 declared, 16 with the property |
| Roles generated implicitly by SAM (an `AWS::Serverless::Function` with no explicit `Role:` and no `PermissionsBoundary` property) | 16, none of them with a boundary |
| …of those, declared in `lma-main.yaml` itself | 3 |
| Nested stacks that `lma-main.yaml` passes `PermissionsBoundaryArn` down to | 6 of 14 |
| Role creations that would end up carrying a non-empty boundary | roughly 47 of roughly 118 |

One of those rows is a hard blocker rather than a gap to fill in later. **`lma-main.yaml` declares 3 of the implicitly-generated roles itself**, so a service role that requires the boundary fails on the *root* stack, before any nested stack is created. There is no partial-adoption path around that one — it has to be fixed first.

An `AWS::Serverless::Function` *can* set a boundary on its generated role, through the function's own `PermissionsBoundary` property; none of the 16 in LMA set it today, so each needs that one property added (not a conversion to an explicit role).

The eight nested stacks that do not receive the value are the LLM-template, chat-button-config, nova-sonic-config, transcript-knowledge-base, meeting-assist setup, VPC, browser-extension and desktop-capture stacks. The browser-extension, desktop-capture and meeting-assist templates define boundary properties internally but are never given the ARN. `lma-vpc-stack` has no `PermissionsBoundaryArn` parameter at all, so its flow-logs role cannot take one yet.

### What completing this would take

1. Add the `PermissionsBoundary` property to the 16 `AWS::Serverless::Function` resources with a generated role — starting with the 3 in `lma-main.yaml`, which block everything else.
2. Add the `PermissionsBoundaryArn` parameter to `lma-vpc-stack`, and pass the value to the eight nested stacks that do not currently receive it.
3. Add the `PermissionsBoundary` property to the 48 declared roles that do not yet have it, 35 of them in `lma-ai-stack`.
4. Author the boundary policy itself. It must allow everything the LMA roles legitimately do; a practical starting point is the action lists from the three inline policies in `LMA-Cloudformation-Service-Role.yaml`, narrowed to your account's resources and regions.

### Trying it on a test stack

If you are working on the above, this is the sequence. Do it on a throwaway stack, not on an existing deployment.

1. **Create the boundary policy** (once, by an administrator):

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

### Returning to no boundary

Both halves of the setting can be reverted, and both directions of the transition are granted:

- Adding a boundary to a deployment that already has roles rewrites those roles, so the service role is granted `iam:PutRolePermissionsBoundary` when a boundary is configured.
- Removing it requires CloudFormation to take the boundary off those roles, so `iam:DeleteRolePermissionsBoundary` is granted in the same statement, under the same condition — the boundary can only be removed from a role that currently carries exactly this boundary.

Without that second grant the revert would fail part-way through, and the rollback of that failed update would fail too, leaving the role that deploys the whole solution in `UPDATE_ROLLBACK_FAILED`.

Revert in this order: set the LMA stack's `PermissionsBoundaryArn` back to `""` and let that update finish, *then* redeploy the service role with `PermissionsBoundaryArn=""`. Doing it the other way round removes the grant before the work that needs it.

Note that a boundary configured here constrains what the roles created through this service role may *do*. It does not stop this service role from detaching a policy from, or deleting, a role: `iam:DetachRolePolicy`, `iam:DeleteRolePolicy` and `iam:DeleteRole` stay unconditioned so that CloudFormation can still update and delete stacks containing roles that predate the boundary or never carried one.

## Deploying the Service Role

### Prerequisites

- AWS Administrator access (one-time setup)
- AWS CLI configured with appropriate credentials
- No permissions boundary policy is needed; leave `PermissionsBoundaryArn` empty — see [Permissions boundary](#permissions-boundary-not-yet-supported-end-to-end)

### Via CLI

```bash
cd iam-roles/cloudformation-management/

aws cloudformation deploy \
  --template-file LMA-Cloudformation-Service-Role.yaml \
  --stack-name LMA-CFServiceRole \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <your-region>
```

`PermissionsBoundaryArn` defaults to empty, which is the configuration LMA supports; see
[Permissions boundary](#permissions-boundary-not-yet-supported-end-to-end).

### Via Console

1. Open the AWS CloudFormation console
2. Click **Create stack** → **With new resources (standard)**
3. Select **Upload a template file** and choose `LMA-Cloudformation-Service-Role.yaml`
4. Set **Stack name** to `LMA-CFServiceRole` (or your preferred name)
5. Leave **PermissionsBoundaryArn** blank
6. Click through **Next**, acknowledge IAM capabilities, and **Submit**
7. Wait for `CREATE_COMPLETE`
8. Copy the **ServiceRoleArn** value from the **Outputs** tab

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
- **Permissions boundary** — when `PermissionsBoundaryArn` is supplied, `iam:CreateRole`, `iam:PutRolePolicy` and `iam:AttachRolePolicy` are conditioned on `iam:PermissionsBoundary`, so the roles created for LMA would stay within that boundary policy. LMA cannot yet deploy in that configuration, so the parameter defaults to empty; see [Permissions boundary](#permissions-boundary-not-yet-supported-end-to-end)
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
| **LMA deployment fails on `iam:CreateRole` after setting a boundary** | Expected: LMA does not yet attach a boundary to every role it creates, so the root stack fails first. Redeploy the service role with `PermissionsBoundaryArn=""`. See [Permissions boundary](#permissions-boundary-not-yet-supported-end-to-end) |
| **Boundary policy too narrow** | The boundary caps what the LMA roles can do, so anything it omits is unavailable to them at runtime. Widen the boundary policy rather than removing it |
| **Reverting a boundary leaves the stack in `UPDATE_ROLLBACK_FAILED`** | Revert the LMA stack's `PermissionsBoundaryArn` to `""` and let that finish *before* redeploying the service role without the boundary; the reverse order removes `iam:DeleteRolePermissionsBoundary` while it is still needed |

## Cleanup

```bash
aws cloudformation delete-stack --stack-name LMA-CFServiceRole
```

This removes both the service role and the PassRole policy.
