# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
# Published regions are limited to those where AWS Lambda MicroVMs are
# available, because VPLaunchType now defaults to MICROVM.
#
# `cloudformation:ValidateTemplate` (called by publish for every sub-stack)
# rejects the Virtual Participant template outright in a region that does not
# know AWS::Lambda::MicrovmImage — "Template format error: Unrecognized resource
# types" — regardless of the parameter value. Verified in ap-southeast-2.
#
# MicroVMs are currently available in exactly five regions: us-east-1, us-east-2,
# us-west-2, ap-northeast-1, eu-west-1. ap-southeast-2 (Sydney) was published
# previously and is replaced by ap-northeast-1 (Tokyo) until MicroVMs reach it.
#
# Bucket basename differs by region: the AWS ML Blog buckets exist only in
# us-east-1 and us-west-2, so Tokyo and Ireland publish to the Big Data Blog
# replica buckets (as ap-southeast-2 did).
./publish.sh aws-ml-blog artifacts/lma us-east-1 public
./publish.sh aws-ml-blog artifacts/lma us-west-2 public
./publish.sh aws-bigdata-blog-replica artifacts/lma ap-northeast-1 public
./publish.sh aws-bigdata-blog-replica artifacts/lma eu-west-1 public
make docs-deploy