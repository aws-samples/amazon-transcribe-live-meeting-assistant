#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
MAKEFLAGS += --warn-undefined-variables
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := all
.DELETE_ON_ERROR:
.SUFFIXES:

all: build
.PHONY: all

# you can set your environment in the following files
MAKE_CONFIG_FILE ?= config.mk
-include $(MAKE_CONFIG_FILE)
MAKE_CONFIG_USER_FILE ?= config-$(USER).mk
-include $(MAKE_CONFIG_USER_FILE)

# only the release target can proceed
ifneq (, $(filter build% package% deploy% publish%, $(MAKECMDGOALS)))
ifndef CONFIG_ENV
$(error [ERROR] - the CONFIG_ENV environmental variable is not set.\
 This variable maps to the sam config-env option.\
 You can set this variable and others in your shell's environment or in the \
 $(MAKE_CONFIG_FILE) or $(MAKE_CONFIG_USER_FILE) files)
endif
endif

all: build
.PHONY: all

TEMPLATE_FILE ?= template.yaml
SAMCONFIG_FILE ?= samconfig.toml
SAM_BUILD_DIR ?= .aws-sam
SRC_DIR := src

OUT_DIR ?= out
$(OUT_DIR):
	@echo '[INFO] creating build output dir: [$(@)]'
	mkdir -p '$(@)'

##########################################################################
# Install
#
# Install build dependencies. Should only be needed before first run or
# if updating build dependencies
##########################################################################

PYTHON_VERSION ?= 3.8
# python virtual environment directory
VIRTUALENV_DEV_DIR ?= $(OUT_DIR)/venv-dev
VENV_DEV_CFG := $(VIRTUALENV_DEV_DIR)/pyvenv.cfg
$(VENV_DEV_CFG): | $(OUT_DIR)
	echo "[INFO] Creating python dev virtual env under directory: [$(VIRTUALENV_DEV_DIR)]"
	python$(PYTHON_VERSION) -m venv '$(VIRTUALENV_DEV_DIR)'
install-python-dev-venv: $(VENV_DEV_CFG)
.PHONY: install-python-dev-venv

VIRTUALENV_BUILD_DIR ?= $(OUT_DIR)/venv-build
VENV_BUILD_CFG := $(VIRTUALENV_BUILD_DIR)/pyvenv.cfg
$(VENV_BUILD_CFG): | $(OUT_DIR)
	echo "[INFO] Creating python build virtual env under directory: [$(VIRTUALENV_BUILD_DIR)]"
	python$(PYTHON_VERSION) -m venv '$(VIRTUALENV_BUILD_DIR)'
install-python-build-venv: $(VENV_BUILD_CFG)
.PHONY: install-python-build-venv

install-python-venv: install-python-dev-venv install-python-build-venv
.PHONY: install-python-venv

VIRTUALENV_DEV_BIN_DIR ?= $(VIRTUALENV_DEV_DIR)/bin
VIRTUALENV_BUILD_BIN_DIR ?= $(VIRTUALENV_BUILD_DIR)/bin
PYTHON_REQUIREMENTS_DIR ?= requirements
# supports multiple requirement files for either build or development
PYTHON_BUILD_REQUIREMENTS := $(PYTHON_REQUIREMENTS_DIR)/requirements-build.txt
PYTHON_DEV_REQUIREMENTS := $(PYTHON_REQUIREMENTS_DIR)/requirements-dev.txt

PYTHON_SRC_DEV_REQUIREMENTS := $(PYTHON_DEV_REQUIREMENTS)
PYTHON_SRC_BUILD_REQUIREMENTS := $(PYTHON_BUILD_REQUIREMENTS)
PYTHON_TARGET_DEV_REQUIREMENTS := $(patsubst \
	$(PYTHON_REQUIREMENTS_DIR)/%, \
	$(OUT_DIR)/%, \
	$(PYTHON_SRC_DEV_REQUIREMENTS) \
)
PYTHON_TARGET_BUILD_REQUIREMENTS := $(patsubst \
	$(PYTHON_REQUIREMENTS_DIR)/%, \
	$(OUT_DIR)/%, \
	$(PYTHON_SRC_BUILD_REQUIREMENTS) \
)

$(PYTHON_TARGET_DEV_REQUIREMENTS): $(OUT_DIR)/%: $(PYTHON_REQUIREMENTS_DIR)/% | $(OUT_DIR) $(VENV_DEV_CFG)
	@echo "[INFO] Installing python dev dependencies file: [$(^)]"
	@source '$(VIRTUALENV_DEV_BIN_DIR)/activate' && \
		pip3 install -r $(^) | tee $(@)
install-python-dev-requirements: $(PYTHON_TARGET_DEV_REQUIREMENTS)
.PHONY: install-python-dev-requirements

$(PYTHON_TARGET_BUILD_REQUIREMENTS): $(OUT_DIR)/%: $(PYTHON_REQUIREMENTS_DIR)/% | $(OUT_DIR) $(VENV_BUILD_CFG)
	@echo "[INFO] Installing python build dependencies file: [$(^)]"
	@source '$(VIRTUALENV_BUILD_BIN_DIR)/activate' && \
		pip3 install -r $(^) | tee $(@)
install-python-build-requirements: $(PYTHON_TARGET_BUILD_REQUIREMENTS)
.PHONY: install-python-build-requirements

install-python-requirements: install-python-dev-requirements install-python-build-requirements
.PHONY: install-python-requirements

ifeq ($(HAS_JS),true)
PACKAGE_JSON := package.json
$(OUT_DIR)/$(PACKAGE_JSON): $(PACKAGE_JSON) | $(OUT_DIR)
	@echo "[INFO] Installing node dependencies file: [$(^)]"
	npm install | tee '$(@)'

install-node-dependencies: $(OUT_DIR)/$(PACKAGE_JSON)
endif
.PHONY: install-node-dependencies

BINFMT_MULTIARCH := /proc/sys/fs/binfmt_misc/qemu-arm
$(BINFMT_MULTIARCH): | $(OUT_DIR)
	docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

install-docker-multiarch: $(BINFMT_MULTIARCH)
.PHONY: install-docker-multiarch

install: install-python-venv install-python-requirements install-node-dependencies
.PHONY: install

# prepend python virtual env bin directories to path
VIRTUALENV_DEV_BIN_DIR ?= "$(VIRTUALENV_DEV_DIR)/bin"
VIRTUALENV_BUILD_BIN_DIR ?= "$(VIRTUALENV_BUILD_DIR)/bin"
export PATH := "$(VIRTUALENV_BUILD_BIN_DIR):$(VIRTUALENV_DEV_BIN_DIR):$(PATH)"
export VIRTUALENV_DIR

##########################################################################
# build
##########################################################################
SAM_CMD ?= $(VIRTUALENV_BUILD_BIN_DIR)/sam

LAMBDA_FUNCTIONS_DIR ?= $(SRC_DIR)/lambda_functions
LAMBDA_FUNCTIONS := $(wildcard $(LAMBDA_FUNCTIONS_DIR)/*)
LAMBDA_FUNCTIONS_PYTHON_SRC_FILES := $(wildcard \
	$(LAMBDA_FUNCTIONS_DIR)/**/*.py \
	$(LAMBDA_FUNCTIONS_DIR)/**/**/*.py \
	$(LAMBDA_FUNCTIONS_DIR)/**/requirements.txt \
)
# python Lambda function dir should have an __init__.py file
LAMBDA_FUNCTIONS_PYTHON_SRC_DIRS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%/__init__.py, \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(wildcard $(LAMBDA_FUNCTIONS_DIR)/**/__init__.py) \
)

ifeq ($(HAS_JS),true)
LAMBDA_FUNCTIONS_JS_SRC_FILES := $(wildcard \
	$(LAMBDA_FUNCTIONS_DIR)/**/*.js \
	$(LAMBDA_FUNCTIONS_DIR)/**/**/*.js \
	$(LAMBDA_FUNCTIONS_DIR)/**/package.json \
)
# JavaScrip Lambda function dir should have a package.json file
LAMBDA_FUNCTIONS_JS_SRC_DIRS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%/package.json, \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(wildcard $(LAMBDA_FUNCTIONS_DIR)/**/package.json) \
)
endif

LAMBDA_LAYERS_DIR ?= $(SRC_DIR)/lambda_layers
LAMBDA_LAYERS := $(wildcard $(LAMBDA_LAYERS_DIR)/*)
LAMBDA_LAYERS_SRC_FILES := $(wildcard \
	$(LAMBDA_LAYERS_DIR)/**/*.py \
	$(LAMBDA_LAYERS_DIR)/**/**/*.py \
	$(LAMBDA_LAYERS_DIR)/**/requirements.txt \
	$(LAMBDA_LAYERS_DIR)/**/Makefile \
)
ifeq ($(HAS_JS),true)
LAMBDA_LAYERS_SRC_FILES += $(wildcard \
	$(LAMBDA_LAYERS_DIR)/**/*.js \
	$(LAMBDA_LAYERS_DIR)/**/**/*.js \
	$(LAMBDA_LAYERS_DIR)/**/package.json \
)
endif
STATE_MACHINES_DIR := $(SRC_DIR)/state_machines
STATE_MACHINES := $(wildcard $(STATE_MACHINES_DIR)/*)
STATE_MACHINES_SRC_FILES := $(wildcard \
	$(STATE_MACHINES_DIR)/**/*.asl.json \
)
BUILD_SOURCES := $(TEMPLATE_FILE) \
	$(LAMBDA_LAYERS_SRC_FILES) \
	$(LAMBDA_FUNCTIONS_PYTHON_SRC_FILES) \
	$(STATE_MACHINES_SRC_FILES) \
	$(SAMCONFIG_FILE) \

ifeq ($(HAS_JS),true)
BUILD_SOURCES += $(LAMBDA_FUNCTIONS_JS_SRC_FILES)
endif

SAM_BUILD_TOML_FILE := $(SAM_BUILD_DIR)/build.toml
$(SAM_BUILD_TOML_FILE): $(BUILD_SOURCES) $(BINFMT_MULTIARCH) | $(SAM_CMD)
	@echo '[INFO] sam building config env: [$(CONFIG_ENV)]'
	'$(SAM_CMD)' build \
		--config-file '$(realpath $(SAMCONFIG_FILE))' \
		--config-env '$(CONFIG_ENV)' \
		--template-file '$(TEMPLATE_FILE)'

SAM_BUILD_TEMPLATE_FILE := $(SAM_BUILD_DIR)/build/template.yaml
$(SAM_BUILD_TEMPLATE_FILE): $(SAM_BUILD_TOML_FILE)

build: $(SAM_BUILD_TEMPLATE_FILE)
.PHONY: build

##########################################################################
# package
##########################################################################
PACKAGE_OUT_FILE := $(OUT_DIR)/template-packaged-$(CONFIG_ENV).yaml
$(PACKAGE_OUT_FILE): $(TEMPLATE_FILE) $(SAM_BUILD_TEMPLATE_FILE) | $(OUT_DIR) $(SAM_CMD)
	@echo '[INFO] sam packaging config env: [$(CONFIG_ENV)]'
	'$(SAM_CMD)' package \
		--config-file '$(realpath $(SAMCONFIG_FILE))' \
		--config-env '$(CONFIG_ENV)' \
		--output-template-file '$(@)'

package: $(PACKAGE_OUT_FILE)
.PHONY: package

##########################################################################
# publish
##########################################################################
PUBLISH_OUT_FILE := $(OUT_DIR)/sam-publish-$(CONFIG_ENV).txt
$(PUBLISH_OUT_FILE): $(PACKAGE_OUT_FILE) | $(OUT_DIR) $(SAM_CMD)
	@echo '[INFO] sam publishing config env: [$(CONFIG_ENV)]'
	'$(SAM_CMD)' publish \
		--debug \
		--config-file '$(realpath $(SAMCONFIG_FILE))' \
		--config-env '$(CONFIG_ENV)' \
		--template '$(PACKAGE_OUT_FILE)' \
	| tee '$(@)'

publish: $(PUBLISH_OUT_FILE)
.PHONY: publish

##########################################################################
# release packaged template and artifacts to an S3 bucket in a region
# AWS_REGION=us-east-1 RELEASE_S3_BUCKET_BASE=my-release-bucket make release
#
# The packaged template and git staged files are uploaded to the bucket
# It replaces the default bootstrap bucket variables in the template
#
# Uses sam to build and package the template (does not depend on samconfig.toml)
#
# The AWS_REGION variable is appended to the RELEASE_S3_BUCKET_BASE so there
# should be an existing bucket with that region name as a suffix. For example:
#
# AWS_REGION=us-east-1 RELEASE_S3_BUCKET_BASEr=my-bucket -> existing bucket: my-bucket-us-east-1
# You can also override the bucket name by setting the RELEASE_S3_BUCKET variable
#
# RELEASE_S3_PREFIX defaults to 'release'
# RELEASE_VERSION defaults to content of VERSION file
#
# RELEASE_S3_BUCKET_BASE=mybucket RELEASE_S3_PREFIX=release RELEASE_VERSION=0.1.0 make release
#
##########################################################################
VERSION_FILE ?= VERSION
AWS_REGION ?= us-east-1
RELEASE_S3_BUCKET := $(RELEASE_S3_BUCKET_BASE)-$(AWS_REGION)
RELEASE_S3_PREFIX ?= release
RELEASE_VERSION ?= $(shell cat $(VERSION_FILE))

# substitute slashes with underscore for filenames
RELEASE_S3_PREFIX_SUB := $(subst /,_,$(RELEASE_S3_PREFIX))

PACKAGE_RELEASE_REPLACE_OUT_FILE := $(OUT_DIR)/template-replaced-$(RELEASE_S3_BUCKET)-$(RELEASE_S3_PREFIX_SUB)-$(RELEASE_VERSION).yaml
$(PACKAGE_RELEASE_REPLACE_OUT_FILE): $(TEMPLATE_FILE) | $(OUT_DIR)
	@[ -z '$(RELEASE_S3_BUCKET_BASE)' ] && \
	  echo '[ERROR] need to set env var: RELEASE_S3_BUCKET_BASE' && \
	  exit 1 || true
	@echo '[INFO] replacing default bootstrap values - BUCKET_BASE_NAME: [$(RELEASE_S3_BUCKET_BASE)] - REGION: [$(AWS_REGION)] - S3_PREFIX: [$(RELEASE_S3_PREFIX)] - VERSION: [$(RELEASE_VERSION)]'
	sed -E \
		" \
		/^ {2,}BootstrapBucketBaseName:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 $(RELEASE_S3_BUCKET_BASE)@ ; \
		/^ {2,}BootstrapS3Prefix:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 $(RELEASE_S3_PREFIX)@ ; \
		/^ {2,}BootstrapVersion:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 $(RELEASE_VERSION)@ ; \
		s@src-hash.zip@$(shell basename $(BUNDLE_RELEASE_FILE))@g ; \
		" \
		'$(<)' > '$(@)'

PACKAGE_RELEASE_FILE_NAME := template-packaged-$(RELEASE_S3_BUCKET)-$(RELEASE_S3_PREFIX_SUB)-$(RELEASE_VERSION).yaml
PACKAGE_RELEASE_OUT_FILE := $(OUT_DIR)/$(PACKAGE_RELEASE_FILE_NAME)
$(PACKAGE_RELEASE_OUT_FILE): $(PACKAGE_RELEASE_REPLACE_OUT_FILE) | $(OUT_DIR)
	@[ -z '$(RELEASE_S3_BUCKET_BASE)' ] && \
	  echo '[ERROR] need to set env var: RELEASE_S3_BUCKET_BASE' && \
	  exit 1 || true

	@echo '[INFO] configuring docker multiarchitecture for sam cross platform build'
	docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

	@echo '[INFO] sam building template file for release $(RELEASE_VERSION)'
	sam build \
		--use-container \
		--parallel \
		--cached \
		--template-file '$(PACKAGE_RELEASE_REPLACE_OUT_FILE)' \

	@echo '[INFO] sam packaging for release $(RELEASE_VERSION)'
	sam package \
		--s3-bucket '$(RELEASE_S3_BUCKET)' \
		--s3-prefix '$(RELEASE_S3_PREFIX)/$(RELEASE_VERSION)' \
		--output-template-file '$(@)' \

BUNDLE_RELEASE_FILE := $(OUT_DIR)/src-$(shell \
  git ls-files |\
  tr '\n' '\0' |\
  sort -z |\
  xargs -0 sha256sum |\
  sha256sum |\
  cut -d" " -f1 |\
  cut -c1-16 \
  ).zip
BUNDLE_SRC_FILES := $(shell git ls-files)
$(BUNDLE_RELEASE_FILE): $(BUNDLE_SRC_FILES) | $(OUT_DIR)
	@echo '[INFO] creating source bundle from git staged files - [$(@)]'
	@echo $(BUNDLE_SRC_FILES) | xargs zip -@ --filesync '$(@)'

RELEASE_UPLOAD_FILE := $(OUT_DIR)/release-upload-$(PACKAGE_RELEASE_FILE_NAME).txt
RELEASE_TEMPLATE_S3_URL := s3://$(RELEASE_S3_BUCKET)/$(RELEASE_S3_PREFIX)/$(RELEASE_VERSION)/template.yaml
RELEASE_BUNDLE_S3_URL := s3://$(RELEASE_S3_BUCKET)/$(RELEASE_S3_PREFIX)/$(RELEASE_VERSION)/$(shell basename $(BUNDLE_RELEASE_FILE))
$(RELEASE_UPLOAD_FILE): $(PACKAGE_RELEASE_OUT_FILE) $(BUNDLE_RELEASE_FILE) | $(OUT_DIR)
	@echo '[INFO] uploading $(PACKAGE_RELEASE_OUT_FILE) to $(RELEASE_TEMPLATE_S3_URL)'
	aws s3 cp '$(PACKAGE_RELEASE_OUT_FILE)' '$(RELEASE_TEMPLATE_S3_URL)' \
	| tee '$(@)'
	@echo '[INFO] uploading $(BUNDLE_RELEASE_FILE) to $(RELEASE_BUNDLE_S3_URL)'
	aws s3 cp '$(BUNDLE_RELEASE_FILE)' '$(RELEASE_BUNDLE_S3_URL)' \
	| tee -a '$(@)'
	@echo '[INFO] CloudFormation template URL: https://$(RELEASE_S3_BUCKET).s3.amazonaws.com/$(RELEASE_S3_PREFIX)/$(RELEASE_VERSION)/template.yaml'
	@echo '[INFO] CloudFormation Console Launch URL: https://$(AWS_REGION).console.aws.amazon.com/cloudformation/home?region=$(AWS_REGION)#/stacks/create/review?templateURL=https://$(RELEASE_S3_BUCKET).s3.amazonaws.com/$(RELEASE_S3_PREFIX)/$(RELEASE_VERSION)/template.yaml'

release: $(RELEASE_UPLOAD_FILE)
.PHONY: release

##########################################################################
# deploy
##########################################################################
DEPLOY_OUT_FILE := $(OUT_DIR)/sam-deploy-$(CONFIG_ENV).txt
$(DEPLOY_OUT_FILE): $(SAM_BUILD_TOML_FILE) | $(OUT_DIR) $(SAM_CMD)
	@rm -f '$(DELETE_STACK_OUT_FILE)'
	@echo '[INFO] sam deploying config env: [$(CONFIG_ENV)]'
	'$(SAM_CMD)' deploy \
		--config-env '$(CONFIG_ENV)' \
		--config-file '$(realpath $(SAMCONFIG_FILE))' \
		| tee '$(@)'

deploy: $(DEPLOY_OUT_FILE)
.PHONY: deploy

##########################################################################
# delete stack
##########################################################################
DELETE_STACK_OUT_FILE := $(OUT_DIR)/sam-delete-$(CONFIG_ENV).txt
$(DELETE_STACK_OUT_FILE): $(SAMCONFIG_FILE) | $(OUT_DIR)
	@echo "[INFO] deleting stack for config env: [$(CONFIG_ENV)]"
	'$(SAM_CMD)' delete \
		--config-env '$(CONFIG_ENV)' \
		--config-file '$(realpath $(SAMCONFIG_FILE))' \
		| tee '$(@)'
	@rm -f '$(DEPLOY_OUT_FILE)'

delete-stack: $(DELETE_STACK_OUT_FILE)
.PHONY: delete-stack

##########################################################################
# tests
##########################################################################

####
# local invoke
####
TESTS_DIR := tests
EVENTS_DIR := $(TESTS_DIR)/events

# build dynamic targets for sam local invoke
# each lambda function should have a corresponding invoke-local-% target
SAM_INVOKE_TARGETS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	local-invoke-%, \
	$(LAMBDA_FUNCTIONS) \
)
.PHONY: $(SAM_INVOKE_TARGETS)
$(SAM_INVOKE_TARGETS): build

export LOCAL_INVOKE_DEBUG_ARGS ?= \

ifdef DEBUGGER_PY
DEBUG_PORT ?= 5678
export LOCAL_INVOKE_DEBUG_ARGS := --debug-port $(DEBUG_PORT) \
	--debug-args '-m debugpy --listen 0.0.0.0:$(DEBUG_PORT) --wait-for-client'
endif

ifdef DEBUGGER_JS
DEBUG_PORT ?= 5858
export LOCAL_INVOKE_DEBUG_ARGS := --debug-port $(DEBUG_PORT)
endif

# Invoke the default event associated with the lambda function
# for each lambda function, there should be a corresponding
# <CONFIG_ENV>.json file under the tests/events/<lambda_function_dir> directory
# where <lambda_function_dir> matches the directory name under
# src/lambda_functions. For example:
#
# make local-invoke-<lambda_function_dir>
#
# You may override the event file by setting the EVENT_FILE environmental
# variable:
# EVENT_FILE=myevent.json make local-invoke-<lambda_function_dir>
#
# The Lambda functions are invoked using environment variables from the file
# under tests/events/<CONFIG_ENV>-env-vars.json. This passes the --env-vars
# parameter to `sam local invoke`. See:
# https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-using-invoke.html#serverless-sam-cli-using-invoke-environment-file
# You can override the file by setting the ENV_VARS_FILE environmental variable:
#
# ENV_VARS_FILE=my-env-vars.json make local-invoke-<lambda_function_dir>
#
# It parses out the logical resource name from the build.toml file
# For example, to invoke the src/lambda_functions/my_function use:
# make local-invoke-my_function
#
# To debug inside a python Lambda function put debugpy in the function
# requirements.txt under the funtion directory.
# Set the DEBUGGER_PY environmental variable when calling local invoke.
# Setup a VS Code launch task to attach to the debugger:
#        {
#            "name": "Debug SAM Python Lambda debugpy attach",
#            "type": "python",
#            "request": "attach",
#            "port": 5678,
#            "host": "localhost",
#            "pathMappings": [
#                {
#                    "localRoot": "${workspaceFolder}/${relativeFileDirname}",
#                    "remoteRoot": "/var/task"
#                }
#            ],
#        }
#
# To debug the incoming_process function use:
# DEBUGGER_PY=true make local-invoke-my_python_function
#
# To debug inside a Node JS Lambda function
# Set the DEBUGGER_JS environmental variable when calling local invoke.
# Setup a VS Code launch task to attach to the debugger:
#        {
#            "name": "Debug SAM Node JS Lambda attach",
#            "type": "node",
#            "request": "attach",
#            "port": 5858,
#            "host": "localhost",
#            "pathMappings": [
#                {
#                    "localRoot": "${workspaceFolder}/${relativeFileDirname}",
#                    "remoteRoot": "/var/task"
#                }
#            ],
#            "protocol": "inspector",
#            "stopOnEntry": false
#        }
#
# To debug the incoming_process function use:
# DEBUGGER_JS=true make local-invoke-my_node_function
$(SAM_INVOKE_TARGETS): local-invoke-%: $(LAMBDA_FUNCTIONS_DIR)/% | $(OUT_DIR) $(SAM_CMD)
	@FUNCTION_LOGICAL_ID=$$( \
	'$(VIRTUALENV_DEV_BIN_DIR)/python' -c 'import toml; \
	f_defs = ( \
	    toml.load("$(SAM_BUILD_TOML_FILE)") \
	    .get("function_build_definitions") \
	); \
	print( \
	    [f_defs[f]["functions"][0] \
	    for f in f_defs \
	    if f_defs[f]["codeuri"].endswith("/$(*)")] \
	    [0] \
	);' \
	) || { \
		echo -n "[ERROR] failed to parse sam build toml file. "; >&2 \
		echo -n "Check that you have sourced the python virtual env and "; >&2 \
		echo -n "run the command: "; >&2 \
		echo "[pip3 install -r $(PYTHON_REQUIREMENTS_DIR)/requirements-dev.txt]"; >&2 \
		exit 1; \
	} && \
	EVENT_FILE="$${EVENT_FILE:-$(EVENTS_DIR)/$(*)/$(CONFIG_ENV).json}" && \
	ENV_VARS_FILE="$${ENV_VARS_FILE:-$(EVENTS_DIR)/$(CONFIG_ENV)-env-vars.json}" && \
	echo "[INFO] invoking target: [$(@)] function: [$${FUNCTION_LOGICAL_ID}] with event file: [$${EVENT_FILE}]" && \
	'$(SAM_CMD)' local invoke \
		--config-env '$(CONFIG_ENV)' \
		--config-file '$(realpath $(SAMCONFIG_FILE))' \
		--event "$$EVENT_FILE" \
		--env-vars "$$ENV_VARS_FILE" \
		$(LOCAL_INVOKE_DEBUG_ARGS) \
		"$$FUNCTION_LOGICAL_ID" \
		| tee '$(OUT_DIR)/$(@).txt'
	@echo
	@tail '$(OUT_DIR)/$(@).txt' | grep -q -E '^{ *"errorMessage" *:.*"errorType" *:' && { \
		echo "[ERROR] Lambda local invoke returned an error" >&2;\
		exit 1; \
	} || true

test-local-invoke-default: $(SAM_INVOKE_TARGETS)
.PHONY: test-local-invoke-default

test: test-local-invoke-default
.PHONY: test

##########################################################################
# lint
##########################################################################

###
# cfn-lint
###
CFN_LINT_OUT_FILE := $(OUT_DIR)/lint-cfn-lint.txt
$(CFN_LINT_OUT_FILE): $(TEMPLATE_FILE) | $(OUT_DIR)
	@echo '[INFO] running cfn-lint on template: [$(^)]'
	$(VIRTUALENV_DEV_BIN_DIR)/cfn-lint '$(^)' | tee '$(@)'

lint-cfn-lint: $(CFN_LINT_OUT_FILE)
.PHONY: lint-cfn-lint

###
# yamllint
###
YAMLLINT_OUT_FILE := $(OUT_DIR)/lint-yamllint.txt
$(YAMLLINT_OUT_FILE): $(TEMPLATE_FILE) | $(OUT_DIR)
	@echo '[INFO] running yamllint on template: [$(^)]'
	$(VIRTUALENV_DEV_BIN_DIR)/yamllint '$(^)' | tee '$(@)'

lint-yamllint: $(YAMLLINT_OUT_FILE)
.PHONY: lint-yamllint

###
# validate
###
VALIDATE_OUT_FILE := $(OUT_DIR)/lint-validate.txt
$(VALIDATE_OUT_FILE): $(TEMPLATE_FILE) | $(OUT_DIR)
	@echo '[INFO] running sam validate on config env: [$(CONFIG_ENV)]'
	'$(SAM_CMD)' validate \
		--config-env '$(CONFIG_ENV)' \
		--template-file '$(TEMPLATE_FILE)' \
	| tee '$(@)'

lint-validate: $(VALIDATE_OUT_FILE)
.PHONY: lint-validate

###
# cfnnag
###
CFN_NAG_OUT_FILE := $(OUT_DIR)/lint-cfnnag.txt
$(CFN_NAG_OUT_FILE): $(TEMPLATE_FILE) | $(OUT_DIR)
	@echo '[INFO] running cfn_nag on template: [$(^)]'
	docker run -i --rm stelligent/cfn_nag /dev/stdin < '$(^)' | tee '$(@)'

lint-cfn_nag: $(CFN_NAG_OUT_FILE)
.PHONY: lint-cfn_nag

###
# cfn-policy-validator
###
CFN_POLICY_VALIDATOR_OUT_FILE := $(OUT_DIR)/lint-cfn-policy-validator.txt
$(CFN_POLICY_VALIDATOR_OUT_FILE): $(TEMPLATE_FILE) | $(OUT_DIR)
	@echo '[INFO] running cfn-policy-validator with config env: [$(CONFIG_ENV)]'
	@read -r REGION PARAMETERS <<< $$('$(VIRTUALENV_DEV_BIN_DIR)/python' -c 'import toml ; \
		deploy_config = toml.load("$(SAMCONFIG_FILE)").get("$(CONFIG_ENV)").get("deploy").get("parameters"); \
		region = deploy_config.get("region"); parameter_overrides = deploy_config.get("parameter_overrides"); \
		params = " ".join(parameter_overrides); \
		print(region, params)') ; \
	echo "[INFO] running cfn-policy-validator with region: [$$REGION] and parameters [$$PARAMETERS]" ; \
	$(VIRTUALENV_DEV_BIN_DIR)/cfn-policy-validator validate \
		--template-path '$(TEMPLATE_FILE)' \
		--region "$$REGION" \
		--parameters $$PARAMETERS \
		$(CFN_POLICY_VALIDATOR_EXTRA_ARGS) \
	| tee '$(@)'

lint-cfn-policy-validator: $(CFN_POLICY_VALIDATOR_OUT_FILE)
.PHONY: lint-cfn-policy-validator

lint-cfn: lint-cfn-lint
.PHONY: lint-cfn
SHOULD_ENABLE_CNF_NAG ?= true
ifeq ($(SHOULD_ENABLE_CNF_NAG),true)
lint-cfn: lint-cfn_nag
endif
SHOULD_ENABLE_CFN_POLICY_VALIDATOR ?= true
ifeq ($(SHOULD_ENABLE_CFN_POLICY_VALIDATOR),true)
lint-cfn: lint-cfn-policy-validator
endif
SHOULD_ENABLE_CFN_VALIDATE ?= true
ifeq ($(SHOULD_ENABLE_CFN_VALIDATE),true)
lint-cfn: lint-validate
endif
SHOULD_ENABLE_YAMLLINT ?= true
ifeq ($(SHOULD_ENABLE_YAMLLINT),true)
lint-cfn: lint-yamllint
endif

###
# pylint
###

# TODO add Lamda Layers
PYTHON_LINTER_MAX_LINE_LENGTH ?= 100
LAMBDA_PYLINT_TARGETS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(OUT_DIR)/lint-pylint-%.txt, \
	$(LAMBDA_FUNCTIONS_PYTHON_SRC_DIRS) \
)
$(LAMBDA_PYLINT_TARGETS): $(LAMBDA_FUNCTIONS_PYTHON_SRC_FILES)
$(LAMBDA_PYLINT_TARGETS): $(OUT_DIR)/lint-pylint-%.txt: $(LAMBDA_FUNCTIONS_DIR)/% | $(OUT_DIR)
	@echo '[INFO] running pylint on dir: [$(<)]'
	$(VIRTUALENV_DEV_BIN_DIR)/pylint \
		--max-line-length='$(PYTHON_LINTER_MAX_LINE_LENGTH)' \
		'$(<)' \
	| tee '$(@)'

lint-pylint: $(LAMBDA_PYLINT_TARGETS)
.PHONY: lint-pylint

###
# flake8
###
LAMBDA_FLAKE8_TARGETS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(OUT_DIR)/lint-flake8-%.txt, \
	$(LAMBDA_FUNCTIONS_PYTHON_SRC_DIRS) \
)
$(LAMBDA_FLAKE8_TARGETS): $(LAMBDA_FUNCTIONS_PYTHON_SRC_FILES)
$(LAMBDA_FLAKE8_TARGETS): $(OUT_DIR)/lint-flake8-%.txt: $(LAMBDA_FUNCTIONS_DIR)/% | $(OUT_DIR)
	@echo '[INFO] running flake8 on dir: [$(<)]'
	$(VIRTUALENV_DEV_BIN_DIR)/flake8 \
		--max-line-length='$(PYTHON_LINTER_MAX_LINE_LENGTH)' \
		$(<) \
	| tee '$(@)'

lint-flake8: $(LAMBDA_FLAKE8_TARGETS)
.PHONY: lint-flake8

###
# mypy
###
LAMBDA_MYPY_TARGETS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(OUT_DIR)/lint-mypy-%.txt, \
	$(LAMBDA_FUNCTIONS_PYTHON_SRC_DIRS) \
)
$(LAMBDA_MYPY_TARGETS): $(LAMBDA_FUNCTIONS_PYTHON_SRC_FILES)
$(LAMBDA_MYPY_TARGETS): $(OUT_DIR)/lint-mypy-%.txt: $(LAMBDA_FUNCTIONS_DIR)/% | $(OUT_DIR)
	@echo '[INFO] running mypy on dir: [$(<)]'
	$(VIRTUALENV_DEV_BIN_DIR)/mypy \
		$(<) \
	| tee '$(@)'

lint-mypy: $(LAMBDA_MYPY_TARGETS)
.PHONY: lint-mypy

###
# black
###
LAMBDA_BLACK_TARGETS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(OUT_DIR)/lint-black-%.txt, \
	$(LAMBDA_FUNCTIONS_PYTHON_SRC_DIRS) \
)
$(LAMBDA_BLACK_TARGETS): $(LAMBDA_FUNCTIONS_PYTHON_SRC_FILES)
$(LAMBDA_BLACK_TARGETS): $(OUT_DIR)/lint-black-%.txt: $(LAMBDA_FUNCTIONS_DIR)/% | $(OUT_DIR)
	@echo '[INFO] running black on dir: [$(<)]'
	$(VIRTUALENV_DEV_BIN_DIR)/black \
		--check \
		--diff \
		--line-length='$(PYTHON_LINTER_MAX_LINE_LENGTH)' \
		$(<) \
	| tee '$(@)'

lint-black: $(LAMBDA_BLACK_TARGETS)
.PHONY: lint-black

###
# bandit
###
LAMBDA_BANDIT_TARGETS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(OUT_DIR)/lint-bandit-%.txt, \
	$(LAMBDA_FUNCTIONS_PYTHON_SRC_DIRS) \
)
$(LAMBDA_BANDIT_TARGETS): $(LAMBDA_FUNCTIONS_PYTHON_SRC_FILES)
$(LAMBDA_BANDIT_TARGETS): $(OUT_DIR)/lint-bandit-%.txt: $(LAMBDA_FUNCTIONS_DIR)/% | $(OUT_DIR)
	@echo '[INFO] running bandit on dir: [$(<)]'
	$(VIRTUALENV_DEV_BIN_DIR)/bandit \
		--recursive \
		$(<) \
	| tee '$(@)'

lint-bandit: $(LAMBDA_BANDIT_TARGETS)
.PHONY: lint-bandit

lint-python: lint-pylint lint-flake8 lint-mypy lint-black lint-bandit
.PHONY: lint-python

###
# eslint
###
ifeq ($(HAS_JS),true)
LAMBDA_ESLINT_TARGETS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(OUT_DIR)/lint-eslint-%.txt, \
	$(LAMBDA_FUNCTIONS_JS_SRC_DIRS) \
)
$(LAMBDA_ESLINT_TARGETS): $(LAMBDA_FUNCTIONS_JS_SRC_FILES)
$(LAMBDA_ESLINT_TARGETS): $(OUT_DIR)/lint-eslint-%.txt: $(LAMBDA_FUNCTIONS_DIR)/% | $(OUT_DIR)
	@echo '[INFO] running eslint on dir: [$(<)]'
	npx eslint \
		$(<) \
	| tee '$(@)'

lint-eslint: $(LAMBDA_ESLINT_TARGETS)
.PHONY: lint-eslint

###
# prettier
###
LAMBDA_PRETTIER_TARGETS := $(patsubst \
	$(LAMBDA_FUNCTIONS_DIR)/%, \
	$(OUT_DIR)/lint-prettier-%.txt, \
	$(LAMBDA_FUNCTIONS_JS_SRC_DIRS) \
)
$(LAMBDA_PRETTIER_TARGETS): $(LAMBDA_FUNCTIONS_JS_SRC_FILES)
$(LAMBDA_PRETTIER_TARGETS): $(OUT_DIR)/lint-prettier-%.txt: $(LAMBDA_FUNCTIONS_DIR)/% | $(OUT_DIR)
	@echo '[INFO] running prettier on dir: [$(<)]'
	npx prettier --check '$(<)/**/*.js' \
		$(<) \
	| tee '$(@)'

lint-prettier: $(LAMBDA_PRETTIER_TARGETS)
.PHONY: lint-prettier

lint-javascript: lint-eslint lint-prettier
endif # HAS_JS
.PHONY: lint-javascript

###
# State Machine Lint
###
STATELINT_DIR ?= $(OUT_DIR)/statelint
STATELINT ?= $(STATELINT_DIR)/bin/statelint
$(STATELINT): | $(OUT_DIR)
	@echo "[INFO] installing statelint"
	-gem install statelint --install-dir '$(STATELINT_DIR)'

STATELINT_TARGETS := $(patsubst \
	$(STATE_MACHINES_DIR)/%, \
	$(OUT_DIR)/lint-statelint-%.txt, \
	$(STATE_MACHINES) \
)

$(STATELINT_TARGETS): $(STATE_MACHINES_SRC_FILES)
$(STATELINT_TARGETS): $(OUT_DIR)/lint-statelint-%.txt: $(STATE_MACHINES_DIR)/% | $(OUT_DIR)
	@echo "[INFO] Running statelint on file: [$(<)]"
	-@GEM_HOME='$(STATELINT_DIR)' '$(STATELINT)' '$(<)/state_machine.asl.json' \
	| tee '$(@)'

lint-state-machines: $(STATELINT_TARGETS)
.PHONY: lint-state-machines

###
# all linters
###
lint: lint-cfn lint-python lint-javascript lint-state-machines
.PHONY: lint

##########################################################################
# XXX TODO add help
##########################################################################
help:
.PHONY: help

##########################################################################
# clean
##########################################################################
clean-out-dir:
	-[ -d '$(OUT_DIR)' ] && rm -rf '$(OUT_DIR)/'*
.PHONY: clean-out-dir

clean-sam-dir:
	-[ -d '$(SAM_BUILD_DIR)' ] && rm -rf '$(SAM_BUILD_DIR)/'*
.PHONY: clean-sam-dir

# TODO clean docker container images

clean: clean-out-dir clean-sam-dir
.PHONY: clean