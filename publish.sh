#!/bin/bash
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

##############################################################################################
# Publish wrapper — delegates to lma-cli (Python) if available, otherwise falls back to
# the legacy bash implementation (publish-legacy.sh).
#
# Usage: ./publish.sh <cfn_bucket_basename> <cfn_prefix> <region> [public]
#
# To use the new Python implementation:
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

# Try lma-cli first (Python implementation)
if command -v lma-cli &> /dev/null; then
  echo "Using lma-cli (Python) for publish..."
  exec lma-cli publish --bucket-basename "$BUCKET_BASENAME" --prefix "$PREFIX" --region "$REGION" $PUBLIC_FLAG
elif command -v lma &> /dev/null; then
  echo "Using lma (Python) for publish..."
  exec lma publish --bucket-basename "$BUCKET_BASENAME" --prefix "$PREFIX" --region "$REGION" $PUBLIC_FLAG
fi

# Fall back to legacy bash implementation
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEGACY_SCRIPT="${SCRIPT_DIR}/publish-legacy.sh"

if [ -f "$LEGACY_SCRIPT" ]; then
  echo "lma-cli not found. Falling back to legacy publish-legacy.sh..."
  echo "Tip: Install lma-cli for a better experience: make setup-cli"
  echo ""
  exec bash "$LEGACY_SCRIPT" "$@"
else
  echo "ERROR: Neither lma-cli nor publish-legacy.sh found." >&2
  echo "Install lma-cli: pip install -e lib/lma_sdk && pip install -e lib/lma_cli_pkg" >&2
  echo "Or: make setup-cli" >&2
  exit 1
fi
