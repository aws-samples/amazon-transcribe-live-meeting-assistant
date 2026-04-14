# Makefile for LMA code quality, building, and deployment
#
# Run 'make help' to see all available targets.

SHELL := /bin/bash

# Define color codes
RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[1;33m
CYAN := \033[0;36m
BOLD := \033[1m
NC := \033[0m  # No Color

# Virtual environment configuration
VENV_DIR := .venv
# Use the venv python/pip if the venv exists, otherwise fall back to system
ifeq ($(wildcard $(VENV_DIR)/bin/python),)
  PYTHON := $(shell command -v python3 2>/dev/null || echo python)
  PIP := $(shell command -v pip3 2>/dev/null || echo pip)
else
  PYTHON := $(CURDIR)/$(VENV_DIR)/bin/python
  PIP := $(CURDIR)/$(VENV_DIR)/bin/pip
endif

# Project paths
AI_STACK_DIR := lma-ai-stack
UI_DIR := $(AI_STACK_DIR)/source/ui
LAMBDA_FUNCTIONS_DIR := $(AI_STACK_DIR)/source/lambda_functions
LAMBDA_LAYERS_DIR := $(AI_STACK_DIR)/source/lambda_layers
WEBSOCKET_DIR := lma-websocket-transcriber-stack
WEBSOCKET_APP_DIR := $(WEBSOCKET_DIR)/source/app
VP_DIR := lma-virtual-participant-stack
VP_BACKEND_DIR := $(VP_DIR)/backend
VERSION_FILE := VERSION
PYTHON_LINE_LENGTH := 100

# CloudFormation templates to validate
CFN_TEMPLATES := \
	lma-main.yaml \
	$(AI_STACK_DIR)/deployment/lma-ai-stack.yaml \
	$(AI_STACK_DIR)/deployment/virtual-participant-enhancements.yaml \
	lma-bedrockagent-stack/template.yaml \
	lma-bedrockkb-stack/template.yaml \
	lma-chat-button-config-stack/deployment/chat-button-config.yaml \
	lma-cognito-stack/deployment/lma-cognito-stack.yaml \
	lma-llm-template-setup-stack/deployment/llm-template-setup.yaml \
	lma-meetingassist-setup-stack/template.yaml \
	lma-nova-sonic-config-stack/deployment/nova-sonic-config.yaml \
	$(VP_DIR)/template.yaml \
	lma-vpc-stack/template.yaml \
	$(WEBSOCKET_DIR)/deployment/lma-websocket-stack.yaml

# Discover Python Lambda function directories (those with .py files)
LAMBDA_FUNCTION_DIRS := $(sort $(dir $(wildcard $(LAMBDA_FUNCTIONS_DIR)/*/*.py)))

##@ General
.PHONY: help
help: ## Show this help message
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; section=""} \
		/^##@/ { section=substr($$0, 5); next } \
		/^[a-zA-Z_-]+:.*?## / { \
			if (section != "" && section != last_section) { \
				printf "\n  \033[1m%s\033[0m\n", section; \
				last_section = section \
			}; \
			printf "  \033[36m%-25s\033[0m %s\n", $$1, $$2 \
		}' $(MAKEFILE_LIST)
	@echo ""

# Default target
.DEFAULT_GOAL := all
all: lint ## Run all linting (default)

# Required Node.js version (major) - must match .nvmrc
NODE_VERSION := 20

##@ Setup
setup: setup-node setup-python ## Set up dev environment (Node version, Python venv)
	@echo ""
	@echo -e "$(GREEN)✅ Full setup complete!$(NC)"

setup-node: ## Ensure correct Node.js version via nvm (installs if needed)
	@CURRENT=$$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/'); \
	if [ "$$CURRENT" = "$(NODE_VERSION)" ]; then \
		echo -e "$(GREEN)✅ Node.js v$$(node -v) already active$(NC)"; \
	else \
		echo "Current Node.js: v$${CURRENT:-not found} (need v$(NODE_VERSION))"; \
		if [ -s "$$HOME/.nvm/nvm.sh" ]; then \
			echo "Using nvm to switch to Node $(NODE_VERSION)..."; \
			source "$$HOME/.nvm/nvm.sh" && nvm install $(NODE_VERSION) && nvm use $(NODE_VERSION); \
			echo -e "$(GREEN)✅ Switched to Node.js $$(node -v)$(NC)"; \
			echo -e "$(YELLOW)   Run 'nvm use $(NODE_VERSION)' in your shell, or add to .zshrc/.bashrc$(NC)"; \
		else \
			echo -e "$(RED)ERROR: Node.js v$(NODE_VERSION).x is required but v$${CURRENT:-none} is active.$(NC)"; \
			echo -e "$(YELLOW)   Install nvm: https://github.com/nvm-sh/nvm$(NC)"; \
			echo -e "$(YELLOW)   Then run: nvm install $(NODE_VERSION) && nvm use $(NODE_VERSION)$(NC)"; \
			exit 1; \
		fi; \
	fi

setup-python: ## Create .venv and install Python dev/lint dependencies
	@if [ ! -f "$(VENV_DIR)/bin/python" ]; then \
		echo "Creating virtual environment in $(VENV_DIR)..."; \
		PYENV_PYTHON=$$(pyenv which python 2>/dev/null); \
		SYS_PYTHON=$$(command -v python3 2>/dev/null); \
		BASE_PYTHON=$${PYENV_PYTHON:-$$SYS_PYTHON}; \
		if [ -z "$$BASE_PYTHON" ]; then \
			echo -e "$(RED)ERROR: No python3 or pyenv python found. Install Python 3+ first.$(NC)"; \
			exit 1; \
		fi; \
		echo "Using base Python: $$BASE_PYTHON ($$($$BASE_PYTHON --version))"; \
		$$BASE_PYTHON -m venv $(VENV_DIR); \
	else \
		echo "Virtual environment already exists at $(VENV_DIR)"; \
	fi
	@echo "Upgrading pip..."
	$(VENV_DIR)/bin/pip install --upgrade pip
	@echo "Installing Python lint/dev tools..."
	$(VENV_DIR)/bin/pip install \
		bandit \
		black \
		cfn-lint \
		flake8 \
		mypy \
		pylint \
		yamllint \
		boto3-stubs[comprehend,codebuild,dynamodb,lambda,lexv2-runtime,s3,sqs,sns]
	@echo ""
	@echo -e "$(GREEN)✅ Python setup complete! Virtual environment at $(VENV_DIR)$(NC)"
	@echo -e "$(YELLOW)   All 'make' targets will automatically use $(VENV_DIR)/bin/python.$(NC)"
	@echo -e "$(YELLOW)   To activate manually: source $(VENV_DIR)/bin/activate$(NC)"

setup-npm: ## Install npm dependencies for UI, WebSocket, and Virtual Participant
	@echo "Installing UI npm dependencies..."
	cd $(UI_DIR) && npm ci --prefer-offline --no-audit
	@echo ""
	@echo "Installing WebSocket transcriber npm dependencies..."
	cd $(WEBSOCKET_APP_DIR) && npm ci --prefer-offline --no-audit
	@echo ""
	@echo "Installing Virtual Participant npm dependencies..."
	cd $(VP_BACKEND_DIR) && npm ci --prefer-offline --no-audit
	@echo ""
	@echo -e "$(GREEN)✅ npm dependencies installed!$(NC)"

##@ Code Quality
lint: lint-cfn lint-python lint-ui ## Run all linting (cfn, python, UI)
fastlint: lint-cfn lint-python ## Quick lint (skip UI checks)

lint-cfn: ## Validate CloudFormation templates with cfn-lint
	@echo "Running cfn-lint on CloudFormation templates..."
	@FAILED=0; \
	for template in $(CFN_TEMPLATES); do \
		if [ -f "$$template" ]; then \
			echo "  Checking $$template..."; \
			if ! cfn-lint --non-zero-exit-code error "$$template" > /dev/null 2>&1; then \
				echo -e "$(RED)  FAIL: $$template$(NC)"; \
				cfn-lint --non-zero-exit-code error "$$template"; \
				FAILED=1; \
			fi; \
		else \
			echo -e "$(YELLOW)  SKIP: $$template (not found)$(NC)"; \
		fi; \
	done; \
	if [ $$FAILED -eq 0 ]; then \
		echo -e "$(GREEN)✅ All CloudFormation templates passed cfn-lint!$(NC)"; \
	else \
		echo -e "$(RED)❌ Some CloudFormation templates have cfn-lint errors$(NC)"; \
		exit 1; \
	fi

lint-python: ## Lint Python Lambda functions with ruff
	@echo "Running ruff on Lambda functions..."
	cd $(AI_STACK_DIR) && ruff check --fix $(CURDIR)/$(LAMBDA_FUNCTIONS_DIR)
	cd $(AI_STACK_DIR) && ruff format $(CURDIR)/$(LAMBDA_FUNCTIONS_DIR)
	@echo -e "$(GREEN)✅ All Python linting passed!$(NC)"

lint-bandit: ## Run bandit security scan on Python Lambda functions
	@echo "Running bandit security scan..."
	bandit --recursive $(LAMBDA_FUNCTIONS_DIR)
	@echo -e "$(GREEN)✅ Bandit security scan passed!$(NC)"

lint-mypy: ## Run mypy type checking on Python Lambda functions
	@echo "Running mypy type checks..."
	mypy --config-file $(AI_STACK_DIR)/mypy.ini $(LAMBDA_FUNCTIONS_DIR)
	@echo -e "$(GREEN)✅ mypy type checks passed!$(NC)"

lint-ui: ## Lint React UI (ESLint)
	@echo "Running UI lint..."
	@cd $(UI_DIR) && npm ci --prefer-offline --no-audit 2>/dev/null && npm run lint
	@echo -e "$(GREEN)✅ UI lint passed!$(NC)"

lint-typescript: ## TypeScript build check on WebSocket and Virtual Participant stacks
	@echo "Running TypeScript build check on WebSocket transcriber..."
	@cd $(WEBSOCKET_APP_DIR) && npm ci --prefer-offline --no-audit 2>/dev/null && npm run build
	@echo "Running TypeScript build check on Virtual Participant..."
	@cd $(VP_BACKEND_DIR) && npm ci --prefer-offline --no-audit 2>/dev/null && npm run build
	@echo -e "$(GREEN)✅ All TypeScript builds succeeded!$(NC)"

format: ## Format Python code with ruff
	@echo "Formatting Python Lambda functions with ruff..."
	cd $(AI_STACK_DIR) && ruff format $(CURDIR)/$(LAMBDA_FUNCTIONS_DIR)
	@echo -e "$(GREEN)✅ Python code formatted!$(NC)"

lint-cicd: ## CI/CD lint — checks only, no modifications
	@echo "Running code quality checks (CI/CD mode — no auto-fix)..."
	@if ! cfn-lint --non-zero-exit-code error $(AI_STACK_DIR)/deployment/lma-ai-stack.yaml; then \
		echo -e "$(RED)ERROR: cfn-lint failed!$(NC)"; \
		exit 1; \
	fi
	@if ! (cd $(AI_STACK_DIR) && ruff check $(CURDIR)/$(LAMBDA_FUNCTIONS_DIR)); then \
		echo -e "$(RED)ERROR: Ruff linting failed!$(NC)"; \
		echo -e "$(YELLOW)Run 'make lint-python' locally to fix these issues.$(NC)"; \
		exit 1; \
	fi
	@if ! (cd $(AI_STACK_DIR) && ruff format --check $(CURDIR)/$(LAMBDA_FUNCTIONS_DIR)); then \
		echo -e "$(RED)ERROR: Code formatting check failed!$(NC)"; \
		echo -e "$(YELLOW)Run 'make format' locally to fix these issues.$(NC)"; \
		exit 1; \
	fi
	@if ! make lint-ui; then \
		echo -e "$(RED)ERROR: UI lint failed$(NC)"; \
		exit 1; \
	fi
	@echo -e "$(GREEN)All code quality checks passed!$(NC)"

##@ Building
build: build-ui build-websocket build-vp ## Build all stacks

build-ui: ## Build React UI for production
	@echo "Building React UI..."
	cd $(UI_DIR) && npm ci --prefer-offline --no-audit && npm run build
	@echo -e "$(GREEN)✅ UI build complete!$(NC)"

build-websocket: ## Build WebSocket transcriber (TypeScript)
	@echo "Building WebSocket transcriber..."
	cd $(WEBSOCKET_APP_DIR) && npm ci --prefer-offline --no-audit && npm run build
	@echo -e "$(GREEN)✅ WebSocket transcriber build complete!$(NC)"

build-vp: ## Build Virtual Participant (TypeScript)
	@echo "Building Virtual Participant..."
	cd $(VP_BACKEND_DIR) && npm ci --prefer-offline --no-audit && npm run build
	@echo -e "$(GREEN)✅ Virtual Participant build complete!$(NC)"

##@ Testing
test: test-ui ## Run all tests

test-ui: ## Run React UI tests
	@echo "Running UI tests..."
	cd $(UI_DIR) && npm ci --prefer-offline --no-audit && CI=true npm test -- --watchAll=false
	@echo -e "$(GREEN)✅ UI tests passed!$(NC)"

##@ UI Development
# Usage: make ui-start STACK_NAME=<stack-name>
ui-start: ## Start UI dev server (requires STACK_NAME for .env generation)
ifndef STACK_NAME
	$(error STACK_NAME is not set. Usage: make ui-start STACK_NAME=<your-stack-name>)
endif
	@if [ -n "$(STACK_NAME)" ]; then \
		echo "Retrieving .env configuration from stack $(STACK_NAME)..."; \
		ENV_CONTENT=$$(aws cloudformation describe-stacks \
			--stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='LocalUITestingEnv'].OutputValue" \
			--output text 2>/dev/null); \
		if [ -z "$$ENV_CONTENT" ] || [ "$$ENV_CONTENT" = "None" ]; then \
			echo -e "$(RED)ERROR: Could not retrieve LocalUITestingEnv from stack $(STACK_NAME)$(NC)"; \
			echo -e "$(YELLOW)Make sure the stack exists and has completed deployment.$(NC)"; \
			exit 1; \
		fi; \
		echo "$$ENV_CONTENT" | sed 's/ \(REACT_APP_\)/\n\1/g' > $(UI_DIR)/.env; \
		echo -e "$(GREEN)✅ Created $(UI_DIR)/.env from stack outputs$(NC)"; \
	fi
	@if [ ! -f $(UI_DIR)/.env ]; then \
		echo -e "$(RED)ERROR: $(UI_DIR)/.env not found$(NC)"; \
		echo -e "$(YELLOW)Either provide STACK_NAME to auto-generate, or create .env manually.$(NC)"; \
		echo -e "$(YELLOW)Usage: make ui-start STACK_NAME=<your-stack-name>$(NC)"; \
		exit 1; \
	fi
	@echo "Installing UI dependencies..."
	cd $(UI_DIR) && npm ci --prefer-offline --no-audit
	@echo "Starting UI development server..."
	cd $(UI_DIR) && npm run start

##@ Publishing & Deployment
# Usage: make publish BUCKET=<bucket-basename> PREFIX=<prefix> REGION=<region> [PUBLIC=true]
publish: ## Run publish.sh to build and upload all artifacts to S3
ifndef BUCKET
	$(error BUCKET is not set. Usage: make publish BUCKET=<bucket-basename> PREFIX=<prefix> REGION=<region>)
endif
ifndef PREFIX
	$(error PREFIX is not set. Usage: make publish BUCKET=<bucket-basename> PREFIX=<prefix> REGION=<region>)
endif
ifndef REGION
	$(error REGION is not set. Usage: make publish BUCKET=<bucket-basename> PREFIX=<prefix> REGION=<region>)
endif
	@echo "Publishing LMA artifacts..."
	@if [ "$(PUBLIC)" = "true" ]; then \
		bash publish.sh $(BUCKET) $(PREFIX) $(REGION) public; \
	else \
		bash publish.sh $(BUCKET) $(PREFIX) $(REGION); \
	fi

##@ Version Management
# Usage: make version V=0.3.1
.PHONY: version
version: ## Update version in VERSION file (Usage: make version V=x.y.z)
ifndef V
	$(error VERSION is not set. Usage: make version V=x.y.z)
endif
	@echo "$(V)" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+' || \
		(echo -e "$(RED)ERROR: '$(V)' is not a valid version. Use format: x.y.z$(NC)" && exit 1)
	@echo "Updating version to $(V)..."
	@echo "$(V)" > $(VERSION_FILE)
	@echo -e "$(GREEN)✅ Version updated to $(V) in $(VERSION_FILE)$(NC)"
	@echo -e "$(YELLOW)   Current version: $$(cat $(VERSION_FILE))$(NC)"

##@ Git Workflow
commit: lint test ## Lint, test, then commit and push
	@echo "Committing changes..."
	@git add . && \
	CHANGES=$$(git diff --cached --stat | tail -1) && \
	BRANCH=$$(git rev-parse --abbrev-ref HEAD) && \
	COMMIT_MSG="[$$(cat $(VERSION_FILE))] $$BRANCH: $$CHANGES" && \
	echo "Commit message: $$COMMIT_MSG" && \
	git commit -m "$$COMMIT_MSG" && \
	git push

fastcommit: fastlint ## Fast lint only, then commit and push
	@echo "Committing changes (fast)..."
	@git add . && \
	CHANGES=$$(git diff --cached --stat | tail -1) && \
	BRANCH=$$(git rev-parse --abbrev-ref HEAD) && \
	COMMIT_MSG="[$$(cat $(VERSION_FILE))] $$BRANCH: $$CHANGES" && \
	echo "Commit message: $$COMMIT_MSG" && \
	git commit -m "$$COMMIT_MSG" && \
	git push

##@ Clean
clean: ## Clean all build artifacts
	@echo "Cleaning build artifacts..."
	-rm -rf $(AI_STACK_DIR)/out
	-rm -rf $(AI_STACK_DIR)/.aws-sam
	-rm -rf $(WEBSOCKET_DIR)/out
	-rm -rf $(VP_DIR)/build $(VP_DIR)/dist
	-rm -rf $(VP_BACKEND_DIR)/build $(VP_BACKEND_DIR)/dist
	-rm -rf $(VENV_DIR)
	@echo -e "$(GREEN)✅ Clean complete!$(NC)"

clean-node: ## Clean all node_modules directories
	@echo "Cleaning node_modules directories..."
	-rm -rf $(UI_DIR)/node_modules
	-rm -rf $(WEBSOCKET_APP_DIR)/node_modules
	-rm -rf $(VP_BACKEND_DIR)/node_modules
	-rm -rf node_modules
	@echo -e "$(GREEN)✅ node_modules cleaned!$(NC)"

clean-all: clean clean-node ## Clean everything (build artifacts + node_modules)
