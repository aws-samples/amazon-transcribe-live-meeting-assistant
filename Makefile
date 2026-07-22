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
	$(WEBSOCKET_DIR)/deployment/lma-websocket-transcriber.yaml

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
setup: setup-node setup-python setup-cli-dev ## Set up dev environment (Node version, Python venv, CLI)
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
		virtualenv \
		yamllint \
		boto3-stubs[comprehend,codebuild,dynamodb,lambda,lexv2-runtime,s3,sqs,sns]
	@echo ""
	@echo -e "$(GREEN)✅ Python setup complete! Virtual environment at $(VENV_DIR)$(NC)"
	@echo -e "$(YELLOW)   All 'make' targets will automatically use $(VENV_DIR)/bin/python.$(NC)"
	@echo -e "$(YELLOW)   To activate manually: source $(VENV_DIR)/bin/activate$(NC)"

setup-cli: ## Install LMA SDK and CLI packages into current Python environment
	@echo "Installing LMA SDK..."
	$(CURDIR)/$(VENV_DIR)/bin/pip install -e lib/lma_sdk
	@echo "Installing LMA CLI..."
	$(CURDIR)/$(VENV_DIR)/bin/pip install -e lib/lma_cli_pkg
	@echo -e "$(GREEN)✅ LMA SDK and CLI installed! Run 'lma --help' to get started.$(NC)"

setup-cli-dev: ## Install LMA SDK and CLI with dev/test dependencies
	@echo "Installing LMA SDK with dev dependencies..."
	$(CURDIR)/$(VENV_DIR)/bin/pip install -e "lib/lma_sdk[dev]"
	@echo "Installing LMA CLI with dev dependencies..."
	$(CURDIR)/$(VENV_DIR)/bin/pip install -e "lib/lma_cli_pkg[dev]"
	@echo -e "$(GREEN)✅ LMA SDK and CLI (with test deps) installed!$(NC)"

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

# Checksum file for UI lint change detection
UI_LINT_CHECKSUM_FILE := .ui-lint-checksum

lint-ui: ## Lint React UI (ESLint, skips if source unchanged; use FORCE=1 to bypass cache)
	@NEW_CHECKSUM=$$(find $(UI_DIR)/src -type f \( -name '*.js' -o -name '*.jsx' -o -name '*.ts' -o -name '*.tsx' \) 2>/dev/null | sort | xargs cat 2>/dev/null | sha256sum | awk '{print $$1}'); \
	OLD_CHECKSUM=$$(cat $(UI_LINT_CHECKSUM_FILE) 2>/dev/null || echo ""); \
	if [ -z "$(FORCE)" ] && [ "$$NEW_CHECKSUM" = "$$OLD_CHECKSUM" ]; then \
		echo -e "$(GREEN)✅ UI lint skipped — source unchanged since last run (use FORCE=1 to override)$(NC)"; \
	else \
		if [ -n "$(FORCE)" ]; then echo "Running UI lint (forced)..."; else echo "Running UI lint..."; fi; \
		cd $(UI_DIR) && npm ci --prefer-offline --no-audit 2>/dev/null && npm run lint && \
		echo "$$NEW_CHECKSUM" > $(CURDIR)/$(UI_LINT_CHECKSUM_FILE) && \
		echo -e "$(GREEN)✅ UI lint passed!$(NC)"; \
	fi

lint-ui-force: ## Lint React UI (ignore checksum, always run)
	@$(MAKE) lint-ui FORCE=1

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
test: test-ui test-sdk test-cli test-lambdas ## Run all tests (no AWS required)

test-sdk: ## Run LMA SDK unit tests
	@echo "Running LMA SDK tests..."
	cd lib/lma_sdk && $(PYTHON) -m pytest tests/ -v
	@echo -e "$(GREEN)✅ LMA SDK tests passed!$(NC)"

test-cli: ## Run LMA CLI unit tests
	@echo "Running LMA CLI tests..."
	cd lib/lma_cli_pkg && $(PYTHON) -m pytest tests/ -v
	@echo -e "$(GREEN)✅ LMA CLI tests passed!$(NC)"

# Lambda unit tests live next to each function's source and use local (sibling)
# imports, so each directory must be run with that dir on sys.path — a single
# top-level `pytest lambda_functions/` collides on duplicate module names. This
# target discovers every dir containing test_*.py and runs pytest from within
# it. No AWS required (the suites mock boto3 / set dummy env).
test-lambdas: ## Run all Lambda function unit tests (no AWS; each dir isolated)
	@echo "Running Lambda function unit tests..."
	@FAILED=0; RAN=0; \
	for d in $$(find $(LAMBDA_FUNCTIONS_DIR) -name 'test_*.py' -not -path '*/node_modules/*' -exec dirname {} \; | sort -u); do \
		files=$$(cd "$$d" && ls test_*.py 2>/dev/null); \
		[ -z "$$files" ] && continue; \
		RAN=$$((RAN+1)); \
		echo -e "$(CYAN)  pytest $$d$(NC)"; \
		if ! ( cd "$$d" && $(PYTHON) -m pytest -q $$files ); then FAILED=1; fi; \
	done; \
	if [ $$RAN -eq 0 ]; then echo -e "$(YELLOW)  no lambda tests found$(NC)"; fi; \
	if [ $$FAILED -ne 0 ]; then echo -e "$(RED)❌ Some Lambda tests failed$(NC)"; exit 1; fi; \
	echo -e "$(GREEN)✅ Lambda function tests passed!$(NC)"

# Checksum file for UI test change detection
UI_TEST_CHECKSUM_FILE := .ui-test-checksum

test-ui: ## Run React UI tests (skips if source unchanged)
	@NEW_CHECKSUM=$$(find $(UI_DIR)/src $(UI_DIR)/public -type f \( -name '*.js' -o -name '*.jsx' -o -name '*.ts' -o -name '*.tsx' -o -name '*.css' -o -name '*.json' -o -name '*.html' \) 2>/dev/null | sort | xargs cat 2>/dev/null | sha256sum | awk '{print $$1}'); \
	OLD_CHECKSUM=$$(cat $(UI_TEST_CHECKSUM_FILE) 2>/dev/null || echo ""); \
	if [ "$$NEW_CHECKSUM" = "$$OLD_CHECKSUM" ]; then \
		echo -e "$(GREEN)✅ UI tests skipped — source unchanged since last run$(NC)"; \
	else \
		echo "Running UI tests..."; \
		cd $(UI_DIR) && npm ci --prefer-offline --no-audit && CI=true npm test -- --run && \
		echo "$$NEW_CHECKSUM" > $(CURDIR)/$(UI_TEST_CHECKSUM_FILE) && \
		echo -e "$(GREEN)✅ UI tests passed!$(NC)"; \
	fi

test-ui-force: ## Run React UI tests (ignore checksum, always run)
	@echo "Running UI tests (forced)..."
	cd $(UI_DIR) && npm ci --prefer-offline --no-audit && CI=true npm test -- --run
	@find $(UI_DIR)/src $(UI_DIR)/public -type f \( -name '*.js' -o -name '*.jsx' -o -name '*.ts' -o -name '*.tsx' -o -name '*.css' -o -name '*.json' -o -name '*.html' \) 2>/dev/null | sort | xargs cat 2>/dev/null | sha256sum | awk '{print $$1}' > $(UI_TEST_CHECKSUM_FILE)
	@echo -e "$(GREEN)✅ UI tests passed!$(NC)"

##@ Docker Build Checks
# These target names collide with real paths (e.g. the integ-tests/ dir), so
# declare them PHONY or make treats them as up-to-date files and skips them.
.PHONY: docker-build-check docker-build-check-transcriber docker-build-check-vp \
        docker-build-check-all integ-tests integ-tests-live test-lambdas
# Build the container images the SAME way the in-stack CodeBuild projects do,
# locally, to catch Dockerfile / build-context regressions (e.g. a COPY of a
# renamed/deleted file) in ~1-2 min instead of via a ~40-min deploy that then
# rolls back. The transcriber image runs 'tsc && eslint' at build time, so this
# also catches missing lint/build config. Requires a running Docker daemon.
docker-build-check: docker-build-check-transcriber ## Build container images locally as CodeBuild does (transcriber; use docker-build-check-all for VP too)
	@echo -e "$(GREEN)✅ Docker build check passed!$(NC)"

docker-build-check-transcriber: ## Build the WebSocket transcriber image (fast; runs tsc + eslint 10)
	@command -v docker >/dev/null 2>&1 || { echo -e "$(RED)ERROR: docker not found / daemon not running.$(NC)"; exit 1; }
	@echo -e "$(CYAN)Building transcriber image (source/app/)...$(NC)"
	@cd $(WEBSOCKET_APP_DIR) && docker build --pull -t lma-transcriber-buildcheck:local . \
		&& docker rmi lma-transcriber-buildcheck:local >/dev/null 2>&1 || true
	@echo -e "$(GREEN)✅ Transcriber image built.$(NC)"

docker-build-check-vp: ## Build the Virtual Participant image (heavy; downloads CloakBrowser Chromium)
	@command -v docker >/dev/null 2>&1 || { echo -e "$(RED)ERROR: docker not found / daemon not running.$(NC)"; exit 1; }
	@echo -e "$(CYAN)Building Virtual Participant image (backend/) — this is heavy...$(NC)"
	@cd $(VP_BACKEND_DIR) && docker build --pull -t lma-vp-buildcheck:local . \
		&& docker rmi lma-vp-buildcheck:local >/dev/null 2>&1 || true
	@echo -e "$(GREEN)✅ Virtual Participant image built.$(NC)"

docker-build-check-all: docker-build-check-transcriber docker-build-check-vp ## Build BOTH container images locally
	@echo -e "$(GREEN)✅ All Docker build checks passed!$(NC)"

##@ Integration Testing
# End-to-end tests against a LIVE deployed stack (see integ-tests/README.md and
# the 'integ-tests' Claude skill). Requires the LMA SDK installed in the venv
# (make setup-cli-dev) and AWS creds (AWS_PROFILE=default). Resolves the target
# stack from STACK, else $LMA_STACK_NAME, else 'LMA'.
INTEG_STACK ?= $(or $(STACK),$(LMA_STACK_NAME),LMA)

integ-tests: ## Run integration tests vs a live stack (Usage: make integ-tests STACK=<name>)
	@echo -e "$(CYAN)Running LMA integration tests against stack '$(INTEG_STACK)'...$(NC)"
	@if [ ! -x "$(VENV_DIR)/bin/pytest" ] && ! $(PYTHON) -c "import pytest" 2>/dev/null; then \
		echo -e "$(RED)ERROR: pytest not found. Run 'make setup-cli-dev' first.$(NC)"; exit 1; \
	fi
	@if ! $(PYTHON) -c "import lma_sdk" 2>/dev/null; then \
		echo -e "$(RED)ERROR: lma_sdk not importable. Run 'make setup-cli-dev' first.$(NC)"; exit 1; \
	fi
	$(PYTHON) -m pytest integ-tests/ --stack-name "$(INTEG_STACK)" -m "not live"
	@echo -e "$(GREEN)✅ Integration tests passed against '$(INTEG_STACK)'!$(NC)"

integ-tests-live: ## Integration tests INCLUDING a real VP meeting join (Usage: make integ-tests-live STACK=<name> PLATFORM=ZOOM MEETING_ID=<id> [MEETING_PASSWORD=<pw>])
ifndef MEETING_ID
	$(error MEETING_ID is not set. Usage: make integ-tests-live STACK=<name> PLATFORM=ZOOM MEETING_ID=<id> [MEETING_PASSWORD=<pw>])
endif
	@echo -e "$(CYAN)Running LMA integration tests (incl. live $(or $(PLATFORM),ZOOM) join) against '$(INTEG_STACK)'...$(NC)"
	$(PYTHON) -m pytest integ-tests/ --stack-name "$(INTEG_STACK)" \
		--vp-platform "$(or $(PLATFORM),ZOOM)" \
		--vp-meeting-id "$(MEETING_ID)" \
		--vp-meeting-password "$(MEETING_PASSWORD)"
	@echo -e "$(GREEN)✅ Integration tests (incl. live join) passed against '$(INTEG_STACK)'!$(NC)"

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
		echo "$$ENV_CONTENT" \
			| tr ' ' '\n' \
			> $(UI_DIR)/.env; \
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

##@ Virtual Participant Development
# Usage:
#   make vp-start STACK_NAME=<stack> PLATFORM=<WEBEX|ZOOM|TEAMS|CHIME> MEETING_ID=<id> \
#                 [MEETING_PASSWORD=<pw>] [DEV=1] [REUSE_ENV=1]
#
# Runs the Virtual Participant Docker container locally against a deployed LMA
# stack. See docs/virtual-participant-local-dev.md for the recommended EC2 +
# VSCode Remote-SSH + VNC workflow.
vp-start: ## Run VP locally via Docker (requires STACK_NAME, PLATFORM, MEETING_ID)
ifndef STACK_NAME
	$(error STACK_NAME is not set. Usage: make vp-start STACK_NAME=<stack> PLATFORM=<platform> MEETING_ID=<id> [MEETING_PASSWORD=<pw>] [DEV=1] [REUSE_ENV=1])
endif
ifndef PLATFORM
	$(error PLATFORM is not set. Must be one of: WEBEX, ZOOM, TEAMS, CHIME)
endif
ifndef MEETING_ID
	$(error MEETING_ID is not set)
endif
	@EXTRA_FLAGS=""; \
	if [ "$(DEV)" = "1" ]; then EXTRA_FLAGS="$$EXTRA_FLAGS --dev"; fi; \
	if [ "$(REUSE_ENV)" = "1" ]; then EXTRA_FLAGS="$$EXTRA_FLAGS --reuse-env"; fi; \
	echo -e "$(CYAN)Launching Virtual Participant locally (stack=$(STACK_NAME), platform=$(PLATFORM), id=$(MEETING_ID))$(NC)"; \
	cd $(VP_BACKEND_DIR) && bash local-test.sh $$EXTRA_FLAGS "$(STACK_NAME)" "$(PLATFORM)" "$(MEETING_ID)" "$(MEETING_PASSWORD)"

vp-start-dev: ## Run VP locally in dev mode (auto-reload on src changes); same args as vp-start
	@$(MAKE) vp-start DEV=1 STACK_NAME="$(STACK_NAME)" PLATFORM="$(PLATFORM)" MEETING_ID="$(MEETING_ID)" MEETING_PASSWORD="$(MEETING_PASSWORD)" REUSE_ENV="$(REUSE_ENV)"

vp-start-reuse: ## Run VP locally reusing existing .env.local (keeps manually-set secrets); same args as vp-start
	@$(MAKE) vp-start REUSE_ENV=1 STACK_NAME="$(STACK_NAME)" PLATFORM="$(PLATFORM)" MEETING_ID="$(MEETING_ID)" MEETING_PASSWORD="$(MEETING_PASSWORD)" DEV="$(DEV)"

vp-stop: ## Stop and remove the local VP container (lma-vp-local-test)
	@if docker ps -a --format '{{.Names}}' | grep -q "^lma-vp-local-test$$"; then \
		echo "Stopping and removing lma-vp-local-test..."; \
		docker rm -f lma-vp-local-test; \
		echo -e "$(GREEN)✅ Container removed.$(NC)"; \
	else \
		echo -e "$(YELLOW)No lma-vp-local-test container found.$(NC)"; \
	fi

vp-logs: ## Tail logs for the local VP container (dev mode)
	@docker logs -f lma-vp-local-test

vp-shell: ## Open a shell inside the running local VP container
	@docker exec -it lma-vp-local-test /bin/bash

##@ Security
# Sample Security Review Tool (https://github.com/aws-samples/sample-security-review-tool).
# Suppressions are tracked in .srt/issues.json (committed via negative-gitignore).
# See docs/security-scanning.md.

srt: ## Run full SRT workflow (setup, scan, then prompt to open dashboard)
	@$(MAKE) srt-setup
	@$(MAKE) srt-scan
	@echo ""
	@echo -e "$(CYAN)Open the dashboard to triage findings:$(NC) make srt-fix"

srt-setup: ## Download and configure SRT (pin via SRT_VERSION env var)
	$(PYTHON) scripts/srt/setup.py

srt-scan: ## Run SRT assessment (non-zero exit in CI on open findings)
	$(PYTHON) scripts/srt/run.py

srt-fix: ## Open the SRT dashboard for interactive triage
	$(PYTHON) scripts/srt/fix.py

srt-clean: ## Remove vendored layer trees, .aws-sam, out/, node_modules, scan artifacts (preserves SRT binary, venv, issues.json, .checksum)
	$(PYTHON) scripts/srt/clean.py --apply

srt-clean-preview: ## Show what `make srt-clean` would remove without deleting anything
	$(PYTHON) scripts/srt/clean.py

srt-clean-checksums: ## Remove **/.checksum cache files (forces full rebuild on next make/publish)
	@find . -name .checksum -not -path './.git/*' -print -delete | wc -l | xargs -I{} echo "Removed {} .checksum file(s)"

srt-migrate-dsr: ## Migrate suppressions from .dsr/issues.json → .srt/issues.json (one-shot)
	$(PYTHON) scripts/srt/migrate_dsr_to_srt.py $(if $(FORCE),--force,)

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
version: ## Update version everywhere (Usage: make version V=x.y.z)
ifndef V
	$(error VERSION is not set. Usage: make version V=x.y.z)
endif
	@echo "$(V)" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+' || \
		(echo -e "$(RED)ERROR: '$(V)' is not a valid version. Use format: x.y.z$(NC)" && exit 1)
	@echo "Updating version to $(V)..."
	@# Root VERSION file
	@echo "$(V)" > $(VERSION_FILE)
	@echo "  $(VERSION_FILE)"
	@# LMA SDK
	@sed -i.bak 's/^version = ".*"/version = "$(V)"/' lib/lma_sdk/pyproject.toml && rm -f lib/lma_sdk/pyproject.toml.bak
	@echo "  lib/lma_sdk/pyproject.toml"
	@sed -i.bak 's/^__version__ = ".*"/__version__ = "$(V)"/' lib/lma_sdk/lma_sdk/__init__.py && rm -f lib/lma_sdk/lma_sdk/__init__.py.bak
	@echo "  lib/lma_sdk/lma_sdk/__init__.py"
	@# LMA CLI
	@sed -i.bak 's/^version = ".*"/version = "$(V)"/' lib/lma_cli_pkg/pyproject.toml && rm -f lib/lma_cli_pkg/pyproject.toml.bak
	@echo "  lib/lma_cli_pkg/pyproject.toml"
	@sed -i.bak 's/^__version__ = ".*"/__version__ = "$(V)"/' lib/lma_cli_pkg/lma_cli/__init__.py && rm -f lib/lma_cli_pkg/lma_cli/__init__.py.bak
	@echo "  lib/lma_cli_pkg/lma_cli/__init__.py"
	@echo -e "$(GREEN)✅ Version updated to $(V) in all locations$(NC)"

##@ Git Workflow
commit: lint test ## Lint, test, auto-generate commit message, commit, and push
	@echo "Generating commit message via Bedrock..."
	@git add . && \
	COMMIT_MESSAGE=$$(bash scripts/generate_commit_message.sh) && \
	echo "Commit message: $$COMMIT_MESSAGE" && \
	git commit -m "$$COMMIT_MESSAGE" && \
	git push

fastcommit: ## Auto-generate commit message, commit, and push (no linting)
	@echo "Generating commit message via Bedrock..."
	@git add . && \
	COMMIT_MESSAGE=$$(bash scripts/generate_commit_message.sh) && \
	echo "Commit message: $$COMMIT_MESSAGE" && \
	git commit -m "$$COMMIT_MESSAGE" && \
	git push

##@ Documentation
docs: docs-build ## Build and serve the documentation site locally
	@echo "Starting docs preview server..."
	cd docs-site && npm run preview

docs-setup: ## One-time docs site setup (symlinks + npm install)
	@echo "Setting up documentation site..."
	cd docs-site && bash setup.sh && npm install
	@echo -e "$(GREEN)✅ Docs site setup complete!$(NC)"

docs-build: docs-setup ## Build documentation site (no serve)
	@echo "Ensuring docs have frontmatter..."
	cd docs-site && bash add-frontmatter.sh
	@echo "Syncing sidebar with new docs..."
	cd docs-site && node sync-sidebar.mjs
	@echo "Building documentation site..."
	cd docs-site && npm run build
	@echo -e "$(GREEN)✅ Docs site built! $(NC)"
	@echo "Preview at: http://localhost:4321"

docs-dev: docs-setup ## Start docs dev server with hot reload
	cd docs-site && npm run dev

docs-deploy: docs-build ## Deploy docs to GitHub Pages (from local build)
	@echo "Deploying to GitHub Pages..."
	touch docs-site/dist/.nojekyll
	cd docs-site && npx gh-pages -d dist --dotfiles --repo https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant.git
	@echo -e "$(GREEN)✅ Docs deployed to GitHub Pages!$(NC)"
	@echo "View at: https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/"

##@ Clean
clean: ## Clean all build artifacts
	@echo "Cleaning build artifacts..."
	-rm -rf $(AI_STACK_DIR)/out
	-rm -rf $(AI_STACK_DIR)/.aws-sam
	-rm -rf $(WEBSOCKET_DIR)/out
	-rm -rf $(VP_DIR)/build $(VP_DIR)/dist
	-rm -rf $(VP_BACKEND_DIR)/build $(VP_BACKEND_DIR)/dist
	-rm -rf $(VENV_DIR)
	-rm -f $(UI_TEST_CHECKSUM_FILE)
	-rm -f $(UI_LINT_CHECKSUM_FILE)
	@echo -e "$(GREEN)✅ Clean complete!$(NC)"

clean-node: ## Clean all node_modules directories
	@echo "Cleaning node_modules directories..."
	-rm -rf $(UI_DIR)/node_modules
	-rm -rf $(WEBSOCKET_APP_DIR)/node_modules
	-rm -rf $(VP_BACKEND_DIR)/node_modules
	-rm -rf node_modules
	@echo -e "$(GREEN)✅ node_modules cleaned!$(NC)"

clean-all: clean clean-node ## Clean everything (build artifacts + node_modules)
