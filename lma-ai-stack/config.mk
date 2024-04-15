#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
# source this file from shell to override Makefile
export TEMPLATE_FILE ?= deployment/lma-ai-stack.yaml
# This maps to the sam cli --config-env option - see samconfig.toml
export CONFIG_ENV ?= shared

export HAS_JS ?= true

export LAMBDA_FUNCTIONS_DIR ?= source/lambda_functions

export CFN_POLICY_VALIDATOR_EXTRA_ARGS := --ignore-finding CognitoAuthorizedRole,EXTERNAL_PRINCIPAL
# disabling cfn-policy-validator since the transcriberPoolQueueProcessingTaskDefTaskRoleDefaultPolicy
# now uses an !If conditional which is not supported as of version 0.0.6
export SHOULD_ENABLE_CFN_POLICY_VALIDATOR ?= false
