---
title: "Custom Domain for the Web UI"
---

# Custom Domain for the Web UI

By default LMA serves its web user interface from the CloudFront distribution's
generated domain name, for example `https://d1234abcdefgh.cloudfront.net/`. You
can optionally serve it from your own domain instead, for example
`https://lma.example.com/`.

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Step 1: Request an ACM Certificate in us-east-1](#step-1-request-an-acm-certificate-in-us-east-1)
- [Step 2: Deploy or Update the Stack](#step-2-deploy-or-update-the-stack)
- [Step 3: Create the DNS Record](#step-3-create-the-dns-record)
- [What Changes When a Custom Domain Is Set](#what-changes-when-a-custom-domain-is-set)
- [Removing the Custom Domain](#removing-the-custom-domain)
- [Troubleshooting](#troubleshooting)
- [Related Documentation](#related-documentation)

## Overview

Two optional stack parameters control this feature:

| Parameter | Description |
|-----------|-------------|
| `WebAppCustomDomainName` | The fully qualified domain name to serve the web UI from, for example `lma.example.com`. Leave empty to keep the default CloudFront domain name. |
| `WebAppCustomDomainCertificateArn` | ARN of an ACM certificate covering that name. Must be issued in **us-east-1**. Required when `WebAppCustomDomainName` is set. |

Both default to empty, so existing deployments are unaffected.

## Prerequisites

- A domain you control, with the ability to create DNS records for it.
- An ACM certificate in **us-east-1** covering the name you plan to use.
  CloudFront only accepts viewer certificates from us-east-1, regardless of the
  Region you deploy LMA to.

## Step 1: Request an ACM Certificate in us-east-1

```bash
aws acm request-certificate --region us-east-1 \
  --domain-name lma.example.com \
  --validation-method DNS
```

Create the CNAME record that ACM returns in `DomainValidationOptions`, then wait
for the certificate status to become `ISSUED`:

```bash
aws acm describe-certificate --region us-east-1 \
  --certificate-arn <certificate-arn> \
  --query 'Certificate.Status'
```

A certificate stuck in `PENDING_VALIDATION` means the validation record has not
resolved yet. The stack will fail to deploy if the certificate is not `ISSUED`.

## Step 2: Deploy or Update the Stack

Set both parameters. This works for a new stack and for updating an existing one:

```bash
lma-cli deploy --stack-name LMA --wait \
  -p WebAppCustomDomainName=lma.example.com \
  -p WebAppCustomDomainCertificateArn=arn:aws:acm:us-east-1:111122223333:certificate/abcd1234
```

In the CloudFormation console the parameters are under **Amazon CloudFront
Configuration**.

## Step 3: Create the DNS Record

After the stack completes, read the `WebAppCloudFrontDomainName` stack output and
point your domain at it.

For Route 53, an alias record is preferred (`Z2FDTNDATAQYW2` is the fixed hosted
zone ID CloudFront uses for all distributions):

```bash
aws route53 change-resource-record-sets --hosted-zone-id <your-zone-id> \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "lma.example.com.",
        "Type": "A",
        "AliasTarget": {
          "HostedZoneId": "Z2FDTNDATAQYW2",
          "DNSName": "<WebAppCloudFrontDomainName>.",
          "EvaluateTargetHealth": false
        }
      }
    }]
  }'
```

Repeat with `"Type": "AAAA"` to serve IPv6 clients. For other DNS providers,
create a `CNAME` from your name to the CloudFront domain name.

The original `*.cloudfront.net` URL keeps working after you add a custom domain.

## What Changes When a Custom Domain Is Set

The stack derives every user-facing URL from the custom domain, so the following
all use it automatically:

- The `ApplicationCloudfrontEndpoint` stack output
- The Chrome extension and Desktop Capture App download URLs
- `CloudFrontEndpoint` in the LMA settings parameter, which is embedded in the
  Cognito welcome email sent to users created after deployment
- The MCP server OAuth callback and logout URLs registered on the Cognito app
  client, and the `VITE_OAUTH_CALLBACK_URL` baked into the web app build
- `LMA_WEB_APP_URL` used by the MCP analytics tools to build meeting links

The `WebAppCloudFrontDomainName` output always reports the real CloudFront domain
name, because that is what your DNS record must target.

Two things are intentionally left on the CloudFront domain:

- The **WebSocket endpoint** used for audio streaming. The web app reads it from
  the LMA settings parameter at runtime, and the Content Security Policy already
  allows `wss://*.cloudfront.net`.
- The **Cognito hosted UI domain**, which is an `amazoncognito.com` name.

## Removing the Custom Domain

Update the stack with both parameters set back to empty. The distribution reverts
to the default CloudFront certificate and drops the alternate domain name. Delete
your DNS record afterwards.

## Troubleshooting

**`InvalidViewerCertificate` during deployment.** The certificate is not in
us-east-1, is not `ISSUED`, or does not cover the name in
`WebAppCustomDomainName`. Wildcard certificates work as long as the name is
within the wildcard's scope.

**`CNAMEAlreadyExists` during deployment.** The same alternate domain name is
already attached to another CloudFront distribution. Remove it there first; a
name can only be claimed by one distribution at a time.

**The domain resolves but returns a CloudFront error.** The DNS record exists but
the distribution does not list the name as an alternate domain name. Confirm the
stack deployed with `WebAppCustomDomainName` set, and that the record targets the
`WebAppCloudFrontDomainName` value.

**The certificate expires and renewal fails.** ACM renews DNS-validated
certificates automatically only while the validation CNAME remains in place. Do
not delete it after issuance.

## Related Documentation

- [Prerequisites & Deployment](prerequisites-and-deployment.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
- [Stack Updates & Upgrades](stack-updates-and-upgrades.md)
- [Infrastructure & Security](infrastructure-and-security.md)
