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
# setup-python installs cfn-lint into the venv, which is not on PATH in CI.
ifeq ($(wildcard $(VENV_DIR)/bin/cfn-lint),)
  CFN_LINT := cfn-lint
else
  CFN_LINT := $(CURDIR)/$(VENV_DIR)/bin/cfn-lint
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
ASR_DIR := lma-asr-microvm-stack
ASR_SOURCE_DIR := $(ASR_DIR)/source
VERSION_FILE := VERSION
PYTHON_LINE_LENGTH := 100

# CloudFormation templates to validate
CFN_TEMPLATES := \
	lma-main.yaml \
	$(AI_STACK_DIR)/deployment/lma-ai-stack.yaml \
	$(AI_STACK_DIR)/deployment/virtual-participant-enhancements.yaml \
	$(ASR_DIR)/template.yaml \
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

# Node.js version pinned for development and CI. `.nvmrc` is the single source
# of truth: setup-node installs it via nvm, GitHub Actions reads it through
# `node-version-file`, and .gitlab-ci.yml downloads the same version.
NODE_PINNED_VERSION := $(strip $(shell cat .nvmrc 2>/dev/null))
# Minimum acceptable version, matching the `engines` field of the UI's
# package.json (jsdom 30 needs >= 22.22.2). Any Node at or above this works;
# check-node reports a shortfall in one line, since NPM_CI below runs npm
# quietly and its EBADENGINE warnings are therefore suppressed.
NODE_MIN_VERSION := 22.22.2

# npm install invocation shared by every JS target. `--loglevel=error --no-fund`
# drops npm's notice-level output (funding summaries, transitive `deprecated`
# notices, and peer-dependency/engine warnings for packages we do not control)
# while still surfacing real install failures.
NPM_CI := npm ci --prefer-offline --no-audit --no-fund --loglevel=error

##@ Setup
setup: setup-node setup-python setup-cli-dev ## Set up dev environment (Node version, Python venv, CLI)
	@echo ""
	@echo -e "$(GREEN)✅ Full setup complete!$(NC)"

setup-node: ## Install the Node.js version pinned in .nvmrc via nvm and make it the default
	@CURRENT=$$(node -v 2>/dev/null | sed 's/^v//'); \
	DEFAULT=$$(source "$$HOME/.nvm/nvm.sh" >/dev/null 2>&1 && nvm version default 2>/dev/null | sed 's/^v//'); \
	OK=0; \
	if [ -n "$$CURRENT" ] && \
	   [ "$$(printf '%s\n%s\n' "$(NODE_MIN_VERSION)" "$$CURRENT" | sort -V | head -n1)" = "$(NODE_MIN_VERSION)" ]; then \
		OK=1; \
	fi; \
	if [ $$OK -eq 1 ] && { [ -z "$$DEFAULT" ] || \
	   [ "$$(printf '%s\n%s\n' "$(NODE_MIN_VERSION)" "$$DEFAULT" | sort -V | head -n1)" = "$(NODE_MIN_VERSION)" ]; }; then \
		echo -e "$(GREEN)✅ Node.js v$$CURRENT already active$(NC)"; \
	elif [ -s "$$HOME/.nvm/nvm.sh" ]; then \
		if [ $$OK -eq 1 ]; then \
			echo "Node.js v$$CURRENT is current, but the nvm default is v$$DEFAULT (new shells would use it)."; \
		else \
			echo "Current Node.js: v$${CURRENT:-not found} (need v$(NODE_MIN_VERSION) or later)"; \
		fi; \
		echo "Using nvm to install Node $(NODE_PINNED_VERSION) (pinned in .nvmrc)..."; \
		source "$$HOME/.nvm/nvm.sh" && \
			nvm install $(NODE_PINNED_VERSION) && \
			nvm use $(NODE_PINNED_VERSION) && \
			nvm alias default $(NODE_PINNED_VERSION); \
		echo -e "$(GREEN)✅ Node.js $(NODE_PINNED_VERSION) installed and set as the nvm default$(NC)"; \
		echo -e "$(YELLOW)   This shell still has v$${CURRENT:-none} — run 'nvm use' here, or open a new shell$(NC)"; \
	else \
		echo -e "$(RED)ERROR: Node.js v$(NODE_MIN_VERSION) or later is required but v$${CURRENT:-none} is active.$(NC)"; \
		echo -e "$(YELLOW)   Install nvm: https://github.com/nvm-sh/nvm$(NC)"; \
		echo -e "$(YELLOW)   Then run: nvm install && nvm use   (both read .nvmrc)$(NC)"; \
		exit 1; \
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

.PHONY: check-node
check-node: ## Warn (one line) if the active Node.js is older than NODE_MIN_VERSION
	@CURRENT=$$(node -v 2>/dev/null | sed 's/^v//'); \
	if [ -z "$$CURRENT" ]; then \
		echo -e "$(RED)Node.js not found on PATH (need >= $(NODE_MIN_VERSION))$(NC)"; \
	elif [ "$$(printf '%s\n%s\n' "$(NODE_MIN_VERSION)" "$$CURRENT" | sort -V | head -n1)" != "$(NODE_MIN_VERSION)" ]; then \
		echo -e "$(YELLOW)Node.js v$$CURRENT is older than the required v$(NODE_MIN_VERSION) — run 'nvm use' (or 'make setup-node')$(NC)"; \
	fi

setup-npm: check-node ## Install npm dependencies for UI, WebSocket, and Virtual Participant
	@echo "Installing UI npm dependencies..."
	cd $(UI_DIR) && $(NPM_CI)
	@echo ""
	@echo "Installing WebSocket transcriber npm dependencies..."
	cd $(WEBSOCKET_APP_DIR) && $(NPM_CI)
	@echo ""
	@echo "Installing Virtual Participant npm dependencies..."
	cd $(VP_BACKEND_DIR) && $(NPM_CI)
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
			if ! $(CFN_LINT) --non-zero-exit-code error "$$template" > /dev/null 2>&1; then \
				echo -e "$(RED)  FAIL: $$template$(NC)"; \
				$(CFN_LINT) --non-zero-exit-code error "$$template"; \
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

lint-ui: check-node ## Lint React UI (ESLint, skips if source unchanged; use FORCE=1 to bypass cache)
	@NEW_CHECKSUM=$$(find $(UI_DIR)/src -type f \( -name '*.js' -o -name '*.jsx' -o -name '*.ts' -o -name '*.tsx' \) 2>/dev/null | sort | xargs cat 2>/dev/null | sha256sum | awk '{print $$1}'); \
	OLD_CHECKSUM=$$(cat $(UI_LINT_CHECKSUM_FILE) 2>/dev/null || echo ""); \
	if [ -z "$(FORCE)" ] && [ "$$NEW_CHECKSUM" = "$$OLD_CHECKSUM" ]; then \
		echo -e "$(GREEN)✅ UI lint skipped — source unchanged since last run (use FORCE=1 to override)$(NC)"; \
	else \
		if [ -n "$(FORCE)" ]; then echo "Running UI lint (forced)..."; else echo "Running UI lint..."; fi; \
		cd $(UI_DIR) && $(NPM_CI) && npm run lint && \
		echo "$$NEW_CHECKSUM" > $(CURDIR)/$(UI_LINT_CHECKSUM_FILE) && \
		echo -e "$(GREEN)✅ UI lint passed!$(NC)"; \
	fi

lint-ui-force: ## Lint React UI (ignore checksum, always run)
	@$(MAKE) lint-ui FORCE=1

lint-typescript: ## TypeScript build check on WebSocket and Virtual Participant stacks
	@echo "Running TypeScript build check on WebSocket transcriber..."
	@cd $(WEBSOCKET_APP_DIR) && $(NPM_CI) && npm run build
	@echo "Running TypeScript build check on Virtual Participant..."
	@cd $(VP_BACKEND_DIR) && $(NPM_CI) && npm run build
	@echo -e "$(GREEN)✅ All TypeScript builds succeeded!$(NC)"

format: ## Format Python code with ruff
	@echo "Formatting Python Lambda functions with ruff..."
	cd $(AI_STACK_DIR) && ruff format $(CURDIR)/$(LAMBDA_FUNCTIONS_DIR)
	@echo -e "$(GREEN)✅ Python code formatted!$(NC)"

lint-cicd: ## CI/CD lint — checks only, no modifications
	@echo "Running code quality checks (CI/CD mode — no auto-fix)..."
	@if ! $(CFN_LINT) --non-zero-exit-code error $(AI_STACK_DIR)/deployment/lma-ai-stack.yaml; then \
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

build-ui: check-node ## Build React UI for production
	@echo "Building React UI..."
	cd $(UI_DIR) && $(NPM_CI) && npm run build
	@echo -e "$(GREEN)✅ UI build complete!$(NC)"

build-websocket: ## Build WebSocket transcriber (TypeScript)
	@echo "Building WebSocket transcriber..."
	cd $(WEBSOCKET_APP_DIR) && $(NPM_CI) && npm run build
	@echo -e "$(GREEN)✅ WebSocket transcriber build complete!$(NC)"

build-vp: ## Build Virtual Participant (TypeScript)
	@echo "Building Virtual Participant..."
	cd $(VP_BACKEND_DIR) && $(NPM_CI) && npm run build
	@echo -e "$(GREEN)✅ Virtual Participant build complete!$(NC)"

##@ Testing
test: test-ui test-sdk test-cli test-lambdas test-appsync test-integ-plumbing test-asr ## Run all tests (no AWS required)

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
	for d in $$(find $(LAMBDA_FUNCTIONS_DIR) $(ASR_DIR)/lambda_functions -name 'test_*.py' -not -path '*/node_modules/*' -exec dirname {} \; | sort -u); do \
		files=$$(cd "$$d" && ls test_*.py 2>/dev/null); \
		[ -z "$$files" ] && continue; \
		RAN=$$((RAN+1)); \
		echo -e "$(CYAN)  pytest $$d$(NC)"; \
		if ! ( cd "$$d" && AWS_DEFAULT_REGION=$${AWS_DEFAULT_REGION:-us-east-1} \
			AWS_REGION=$${AWS_REGION:-us-east-1} \
			$(PYTHON) -m pytest -q $$files ); then FAILED=1; fi; \
	done; \
	if [ $$RAN -eq 0 ]; then echo -e "$(YELLOW)  no lambda tests found$(NC)"; fi; \
	if [ $$FAILED -ne 0 ]; then echo -e "$(RED)❌ Some Lambda tests failed$(NC)"; exit 1; fi; \
	echo -e "$(GREEN)✅ Lambda function tests passed!$(NC)"

# Checksum file for UI test change detection
UI_TEST_CHECKSUM_FILE := .ui-test-checksum

test-ui: check-node ## Run React UI tests (skips if source unchanged)
	@NEW_CHECKSUM=$$(find $(UI_DIR)/src $(UI_DIR)/public -type f \( -name '*.js' -o -name '*.jsx' -o -name '*.ts' -o -name '*.tsx' -o -name '*.css' -o -name '*.json' -o -name '*.html' \) 2>/dev/null | sort | xargs cat 2>/dev/null | sha256sum | awk '{print $$1}'); \
	OLD_CHECKSUM=$$(cat $(UI_TEST_CHECKSUM_FILE) 2>/dev/null || echo ""); \
	if [ "$$NEW_CHECKSUM" = "$$OLD_CHECKSUM" ]; then \
		echo -e "$(GREEN)✅ UI tests skipped — source unchanged since last run$(NC)"; \
	else \
		echo "Running UI tests..."; \
		cd $(UI_DIR) && $(NPM_CI) && CI=true npm test -- --run && \
		echo "$$NEW_CHECKSUM" > $(CURDIR)/$(UI_TEST_CHECKSUM_FILE) && \
		echo -e "$(GREEN)✅ UI tests passed!$(NC)"; \
	fi

test-vp: ## Run Virtual Participant backend unit tests (no AWS)
	@echo "Running Virtual Participant backend unit tests..."
	cd $(VP_BACKEND_DIR) && $(NPM_CI) && npm test
	@echo -e "$(GREEN)✅ Virtual Participant unit tests passed!$(NC)"

# Collects the whole directory rather than naming each file: the list used to be
# spelled out here and a new test file only ran in CI if its author remembered to
# append it, which twice they did not. Everything in that directory is a static
# test needing no AWS, and the one module that is not a test suite
# (validate_state_machine.py, a helper) is not named test_* so pytest skips it.
test-vp-template: ## Static tests on the VP template + MicroVM client (no AWS)
	@echo "Running Virtual Participant template + MicroVM client tests..."
	$(PYTHON) -m pytest $(VP_DIR)/test/ -q
	@echo -e "$(GREEN)✅ Virtual Participant template tests passed!$(NC)"

# Collects the directory, not a file list, so a test added here runs in CI
# without anyone having to remember to name it (see test-vp-template).
test-appsync: ## Static AppSync schema/resolver/UI-operation contract tests (no AWS)
	@echo "Running AppSync contract tests..."
	$(PYTHON) -m pytest $(AI_STACK_DIR)/test/ -q
	@echo -e "$(GREEN)✅ AppSync contract tests passed!$(NC)"
# Everything else under integ-tests/ needs a deployed stack. This one file does
# not, and it runs in the fast pipeline because it covers the machinery that
# decides whether a scheduled integration run can report success without having
# tested anything — credential resolution and the --no-skips hook.
test-integ-plumbing: ## Unit tests for the scheduled integ-test machinery (no AWS)
	@echo "Running integration-test plumbing unit tests..."
	$(PYTHON) -m pytest integ-tests/test_ci_plumbing.py -q
	@echo -e "$(GREEN)✅ Integration-test plumbing tests passed!$(NC)"

test-asr: ## Run ASR MicroVM runtime unit tests (no AWS, no model weights)
	@echo "Running ASR MicroVM runtime tests..."
	@test -d $(ASR_SOURCE_DIR)/.venv || $(PYTHON) -m venv $(ASR_SOURCE_DIR)/.venv
	@$(ASR_SOURCE_DIR)/.venv/bin/pip install -q -r $(ASR_SOURCE_DIR)/requirements-dev.txt
	cd $(ASR_SOURCE_DIR) && .venv/bin/python -m pytest -q && .venv/bin/ruff check .
	@echo -e "$(GREEN)✅ ASR MicroVM runtime tests passed!$(NC)"

test-ui-force: check-node ## Run React UI tests (ignore checksum, always run)
	@echo "Running UI tests (forced)..."
	cd $(UI_DIR) && $(NPM_CI) && CI=true npm test -- --run
	@find $(UI_DIR)/src $(UI_DIR)/public -type f \( -name '*.js' -o -name '*.jsx' -o -name '*.ts' -o -name '*.tsx' -o -name '*.css' -o -name '*.json' -o -name '*.html' \) 2>/dev/null | sort | xargs cat 2>/dev/null | sha256sum | awk '{print $$1}' > $(UI_TEST_CHECKSUM_FILE)
	@echo -e "$(GREEN)✅ UI tests passed!$(NC)"

##@ Docker Build Checks
# These target names collide with real paths (e.g. the integ-tests/ dir), so
# declare them PHONY or make treats them as up-to-date files and skips them.
.PHONY: docker-build-check docker-build-check-transcriber docker-build-check-vp \
        docker-build-check-all integ-tests integ-tests-live integ-tests-nightly \
        integ-deploy-and-test test-lambdas \
        test-vp test-vp-template test-vp-microvm-e2e test-appsync \
        test-integ-plumbing test-asr
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

# Container-level e2e for the MicroVM launch path. Needs no AWS: it drives the
# Lambda MicroVMs lifecycle hooks against a locally-run container and asserts
# the things unit tests can't reach — that the pre-snapshot stack really comes
# up (Xvfb/x11vnc/websockify/PulseAudio 3-sink topology), that /ready gates the
# snapshot on health, that /run injects per-meeting config into a real app
# process, that ALB self-registration is skipped, and that secrets stay out of
# logs. Builds the image on first run (heavy), reuses it afterwards.
test-vp-microvm-e2e: ## VP MicroVM launch-path e2e against a local container (no AWS; builds image if absent)
	@command -v docker >/dev/null 2>&1 || { echo -e "$(RED)ERROR: docker not found / daemon not running.$(NC)"; exit 1; }
	@echo -e "$(CYAN)Running VP MicroVM launch-path e2e...$(NC)"
	@bash $(VP_BACKEND_DIR)/test/microvm-e2e.sh lma-vp-microvm-e2e
	@echo -e "$(GREEN)✅ VP MicroVM e2e passed!$(NC)"

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

# The scheduled-pipeline entry point (nightly_integ_tests in .gitlab-ci.yml).
# Differs from integ-tests in exactly two ways, both about a run nobody is
# watching:
#   --no-skips  a skipped test fails the run. The opt-in audio test is the whole
#               reason the schedule exists, and it skips itself when its
#               credentials or optional dependencies are missing, so without this
#               a lapsed secret reports green having tested none of the pipeline.
#   --junit-xml a report the CI UI can show per test, since nobody reads the log
#               of a run that passed.
# The Cognito user comes from LMA_TEST_USER_SECRET_ID (Secrets Manager) rather
# than LMA_TEST_PASSWORD, so the password is never a CI variable — see
# integ-tests/cognito_test_user.py.
INTEG_JUNIT ?= integ-tests-report.xml
integ-tests-nightly: ## Integration tests for the scheduled pipeline: skips fail, JUnit report (Usage: make integ-tests-nightly STACK=<name>)
	@echo -e "$(CYAN)Running scheduled LMA integration tests against '$(INTEG_STACK)'...$(NC)"
	@if ! $(PYTHON) -c "import lma_sdk" 2>/dev/null; then \
		echo -e "$(RED)ERROR: lma_sdk not importable. Run 'make setup-cli-dev' first.$(NC)"; exit 1; \
	fi
	@if ! $(PYTHON) -c "import pycognito, websockets" 2>/dev/null; then \
		echo -e "$(RED)ERROR: pycognito / websockets missing — the audio test would skip.$(NC)"; \
		echo -e "$(YELLOW)   Run: $(PIP) install -r integ-tests/requirements.txt$(NC)"; exit 1; \
	fi
	@if [ -z "$$LMA_TEST_USER_SECRET_ID" ] && [ -z "$$LMA_TEST_USERNAME" ]; then \
		echo -e "$(RED)ERROR: no Cognito test user configured — the audio test would skip.$(NC)"; \
		echo -e "$(YELLOW)   Set LMA_TEST_USER_SECRET_ID (preferred) or LMA_TEST_USERNAME/LMA_TEST_PASSWORD.$(NC)"; \
		exit 1; \
	fi
	$(PYTHON) -m pytest integ-tests/ --stack-name "$(INTEG_STACK)" -m "not live" \
		--no-skips --junit-xml="$(INTEG_JUNIT)"
	@echo -e "$(GREEN)✅ Scheduled integration tests passed against '$(INTEG_STACK)'!$(NC)"

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

# Deploy (create-if-new / update-if-exists) from local code, then run the
# integration tests. This is the one-shot entry point the 'run integration tests'
# Claude skill uses. STACK defaults to 'lma-integtest1' here (not 'LMA') so the
# skill never touches a prod-looking stack by accident. ADMIN_EMAIL is required
# only when the stack does not yet exist.
INTEG_DEPLOY_STACK ?= $(or $(STACK),$(LMA_STACK_NAME),lma-integtest1)
integ-deploy-and-test: ## Deploy (create/update) a stack from local code, then run integ-tests (Usage: make integ-deploy-and-test [STACK=<name>] [ADMIN_EMAIL=<email>])
	@if ! $(PYTHON) -c "import lma_sdk" 2>/dev/null; then \
		echo -e "$(RED)ERROR: lma_sdk not importable. Run 'make setup-cli-dev' first.$(NC)"; exit 1; \
	fi
	@STACK_NAME="$(INTEG_DEPLOY_STACK)"; \
	if aws cloudformation describe-stacks --stack-name "$$STACK_NAME" >/dev/null 2>&1; then \
		echo -e "$(CYAN)Stack '$$STACK_NAME' exists — updating from local code...$(NC)"; \
		$(VENV_DIR)/bin/lma deploy --stack-name "$$STACK_NAME" --from-code . --wait; \
	else \
		if [ -z "$(ADMIN_EMAIL)" ]; then \
			echo -e "$(RED)ERROR: stack '$$STACK_NAME' does not exist and ADMIN_EMAIL is not set.$(NC)"; \
			echo -e "$(YELLOW)Usage: make integ-deploy-and-test STACK=$$STACK_NAME ADMIN_EMAIL=you@example.com$(NC)"; \
			exit 1; \
		fi; \
		echo -e "$(CYAN)Stack '$$STACK_NAME' not found — creating from local code (admin=$(ADMIN_EMAIL))...$(NC)"; \
		$(VENV_DIR)/bin/lma deploy --stack-name "$$STACK_NAME" --from-code . --admin-email "$(ADMIN_EMAIL)" --wait; \
	fi
	@$(MAKE) integ-tests STACK="$(INTEG_DEPLOY_STACK)"

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
	cd $(UI_DIR) && $(NPM_CI)
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
#
# SRT reads and rewrites .srt/issues.json on every assessment, so that file is
# generated output and is gitignored. The reviewed decisions are kept separately
# in .srt/suppressions.json, which IS tracked: `srt-seed` restores issues.json
# from it on a fresh checkout (CI, new clone) so a scan starts from the reviewed
# state, and `srt-save-suppressions` refreshes it after triage.
# See docs/security-scanning.md.
SRT_ISSUES := .srt/issues.json
SRT_SUPPRESSIONS := .srt/suppressions.json

srt: ## Run full SRT workflow (setup, scan, then prompt to open dashboard)
	@$(MAKE) srt-setup
	@$(MAKE) srt-scan
	@echo ""
	@echo -e "$(CYAN)Open the dashboard to triage findings:$(NC) make srt-fix"

srt-setup: ## Download and configure SRT (pin via SRT_VERSION env var)
	$(PYTHON) scripts/srt/setup.py

srt-scan: srt-seed ## Run SRT assessment (non-zero exit in CI on open findings)
	$(PYTHON) scripts/srt/run.py

srt-seed: ## Restore .srt/issues.json from the tracked decisions (no-op if it exists)
	@if [ -f $(SRT_ISSUES) ]; then \
		echo "$(SRT_ISSUES) already present — leaving it as-is."; \
	elif [ -f $(SRT_SUPPRESSIONS) ]; then \
		mkdir -p .srt && cp $(SRT_SUPPRESSIONS) $(SRT_ISSUES); \
		echo -e "$(GREEN)✅ Seeded $(SRT_ISSUES) from $(SRT_SUPPRESSIONS)$(NC)"; \
	else \
		echo -e "$(YELLOW)No $(SRT_SUPPRESSIONS) found — scanning without prior decisions.$(NC)"; \
	fi

srt-save-suppressions: ## Refresh .srt/suppressions.json from .srt/issues.json after triage (then commit it)
	@test -f $(SRT_ISSUES) || { echo -e "$(RED)ERROR: $(SRT_ISSUES) not found. Run 'make srt-scan' first.$(NC)"; exit 1; }
	@$(PYTHON) -c "import json; p='$(SRT_ISSUES)'; q='$(SRT_SUPPRESSIONS)'; d=json.load(open(p)); k=sorted([i for i in d if i.get('status')=='suppressed'], key=lambda i: (str(i.get('source')), str(i.get('check_id')), str(i.get('path')), str(i.get('line')))); f=open(q,'w'); json.dump(k,f,indent=2); f.write('\n'); f.close(); print('Wrote %d decisions to %s (from %d entries in %s)' % (len(k), q, len(d), p))"

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
# Both helpers share _commit-push below. They stage modifications to tracked
# files only (`git add -u`) and refuse to run while any untracked file is
# present: this is a public repository and working trees here routinely contain
# large local scratch directories, so `git add .` is the wrong default. A path
# that only ever exists in your own working tree belongs in .git/info/exclude
# (local, not shared) rather than in .gitignore, which would hide it from
# `git add` and `git status` for every contributor and on every branch — some
# branches legitimately track paths that others treat as scratch. The
# staged diffstat and the generated message are shown and confirmed before
# anything is committed or pushed, and the prompt names the resolved upstream
# (remote + branch) rather than just the local branch, because upstreams differ
# per branch in this repo. Committing straight to develop or main is refused by
# default: changes normally go through a pull request. Pass ALLOW_SHARED_BRANCH=1
# to commit onto a shared branch deliberately. _commit-preflight runs those
# checks up front too, so `make commit` fails in a second rather than after the
# full test run.
.PHONY: commit fastcommit _commit-preflight _commit-push

commit: _commit-preflight lint test ## Lint, test, stage tracked changes, then review + confirm before push
	@$(MAKE) --no-print-directory _commit-push

fastcommit: ## Stage tracked changes, then review + confirm before push (no lint/test)
	@$(MAKE) --no-print-directory _commit-push

_commit-preflight:
	@if [ ! -t 0 ]; then \
		echo -e "$(RED)ERROR: this target needs an interactive terminal (it asks for confirmation).$(NC)"; \
		exit 1; \
	fi
	@BRANCH=$$(git rev-parse --abbrev-ref HEAD); \
	UPSTREAM=$$(git rev-parse --abbrev-ref --symbolic-full-name @{upstream} 2>/dev/null); \
	SHARED=; \
	case "$$BRANCH" in develop|main) SHARED=1;; esac; \
	case "$$UPSTREAM" in */develop|*/main) SHARED=1;; esac; \
	if [ -n "$$SHARED" ] && [ -n "$(ALLOW_SHARED_BRANCH)" ]; then \
		echo -e "$(YELLOW)Committing directly onto $$BRANCH -> $${UPSTREAM:-no upstream} (ALLOW_SHARED_BRANCH set).$(NC)"; \
	elif [ -n "$$SHARED" ]; then \
		echo -e "$(RED)ERROR: refusing to commit onto a shared branch ($$BRANCH -> $${UPSTREAM:-no upstream}).$(NC)"; \
		echo -e "$(YELLOW)Create a branch ('git switch -c feature/<name>') and open a pull request instead,$(NC)"; \
		echo -e "$(YELLOW)or re-run with ALLOW_SHARED_BRANCH=1 to commit here deliberately.$(NC)"; \
		exit 1; \
	fi
	@UNTRACKED=$$(git ls-files --others --exclude-standard); \
	if [ -n "$$UNTRACKED" ]; then \
		echo -e "$(RED)ERROR: untracked files present — refusing to commit.$(NC)"; \
		echo "$$UNTRACKED"; \
		echo -e "$(YELLOW)Stage them explicitly ('git add <path>'), remove them, or — if they are local-only$(NC)"; \
		echo -e "$(YELLOW)paths — add them to .git/info/exclude, then re-run.$(NC)"; \
		exit 1; \
	fi

_commit-push: _commit-preflight
	@git add -u
	@if git diff --cached --quiet; then \
		echo -e "$(YELLOW)Nothing staged — no tracked files changed.$(NC)"; \
		exit 0; \
	fi; \
	TARGET=$$(git rev-parse --abbrev-ref --symbolic-full-name @{upstream} 2>/dev/null); \
	if [ -z "$$TARGET" ]; then \
		TARGET="$$(git rev-parse --abbrev-ref HEAD) (no upstream set — 'git push' will ask for one)"; \
	fi; \
	echo -e "$(CYAN)Staged changes:$(NC)"; \
	git diff --cached --stat; \
	echo "Generating commit message via Bedrock..."; \
	COMMIT_MESSAGE=$$(bash scripts/generate_commit_message.sh) || exit 1; \
	echo -e "$(CYAN)Commit message:$(NC) $$COMMIT_MESSAGE"; \
	read -r -p "Commit the above and push to $$TARGET? [y/N] " REPLY; \
	case "$$REPLY" in \
		[yY]|[yY][eE][sS]) ;; \
		*) echo -e "$(YELLOW)Aborted — changes left staged.$(NC)"; exit 0;; \
	esac; \
	git commit -m "$$COMMIT_MESSAGE" && git push

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
