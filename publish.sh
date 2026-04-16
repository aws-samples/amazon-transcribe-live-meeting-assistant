#!/bin/bash
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

##############################################################################################
# Publish — delegates to lma CLI (Python).
#
# Usage: ./publish.sh <cfn_bucket_basename> <cfn_prefix> <region> [public]
#
# Install the CLI first:
#   pip install -e lib/lma_sdk && pip install -e lib/lma_cli_pkg
#   # or: make setup-cli
##############################################################################################

set -e

USAGE="$0 <cfn_bucket_basename> <cfn_prefix> <region> [public]"

BUCKET_BASENAME=$1
[ -z "$BUCKET_BASENAME" ] && echo "Cfn bucket name is a required parameter. Usage $USAGE" && exit 1

PREFIX=$2
[ -z "$PREFIX" ] && echo "Prefix is a required parameter. Usage $USAGE" && exit 1

REGION=$3
[ -z "$REGION" ] && echo "Region is a required parameter. Usage $USAGE" && exit 1

ACL=$4
PUBLIC_FLAG=""
if [ "$ACL" == "public" ]; then
  PUBLIC_FLAG="--public"
fi

if command -v lma &> /dev/null; then
  exec lma publish --bucket-basename "$BUCKET_BASENAME" --prefix "$PREFIX" --region "$REGION" $PUBLIC_FLAG
elif command -v lma-cli &> /dev/null; then
  exec lma-cli publish --bucket-basename "$BUCKET_BASENAME" --prefix "$PREFIX" --region "$REGION" $PUBLIC_FLAG
fi

echo "ERROR: lma CLI not found." >&2
echo "Install it: pip install -e lib/lma_sdk && pip install -e lib/lma_cli_pkg" >&2
echo "Or: make setup-cli" >&2
exit 1
