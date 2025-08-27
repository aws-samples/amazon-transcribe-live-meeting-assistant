#!/bin/bash
#
# 
# Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
# or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
# 
#
set -a
source .env
envsubst < public/lex-web-ui-loader-config-template.json > public/lex-web-ui-loader-config.json
set +a