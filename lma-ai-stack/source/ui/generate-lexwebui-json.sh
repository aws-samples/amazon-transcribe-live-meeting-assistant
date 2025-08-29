#!/bin/bash
#
# 
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. 
# SPDX-License-Identifier: MIT-0
# 
#
set -a
source .env
envsubst < public/lex-web-ui-loader-config-template.json > public/lex-web-ui-loader-config.json
set +a