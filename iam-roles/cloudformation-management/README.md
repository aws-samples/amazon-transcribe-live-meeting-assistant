# CloudFormation Service Role for LMA

This directory contains a CloudFormation template that creates a dedicated IAM service role for deploying and managing LMA stacks.

## Quick Start

```bash
aws cloudformation deploy \
  --template-file LMA-Cloudformation-Service-Role.yaml \
  --stack-name LMA-CFServiceRole \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <your-region>
```

Then deploy LMA using the service role:

```bash
ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name LMA-CFServiceRole \
  --query 'Stacks[0].Outputs[?OutputKey==`ServiceRoleArn`].OutputValue' \
  --output text)

lma-cli deploy --stack-name MyLMA --admin-email user@example.com --role-arn $ROLE_ARN --wait
```

## Documentation

For full documentation — including Console deployment, PassRole policy assignment, security details, service permissions, and troubleshooting — see the [CloudFormation Service Role guide](../../docs/cloudformation-service-role.md).

## Files

| File | Description |
|------|-------------|
| `LMA-Cloudformation-Service-Role.yaml` | CloudFormation template creating the service role and PassRole policy |
| `README.md` | This file |
