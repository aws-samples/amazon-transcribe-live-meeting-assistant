#!/bin/bash
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
set -a
source .env
envsubst < public/lex-web-ui-loader-config-template.json > public/lex-web-ui-loader-config.json
set +a