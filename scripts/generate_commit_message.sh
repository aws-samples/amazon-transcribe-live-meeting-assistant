#!/bin/bash
# Generate a commit message using AWS Bedrock (no kiro-cli dependency)
#
# Usage:
#   bash scripts/generate_commit_message.sh
#
# Environment variables:
#   COMMIT_MODEL_ID - Bedrock model/inference profile to use (default: us.amazon.nova-lite-v1:0)
#                     Examples:
#                       us.amazon.nova-lite-v1:0          (Nova Lite - default, cheapest)
#                       us.amazon.nova-micro-v1:0         (Nova Micro - fastest)
#                       us.amazon.nova-pro-v1:0           (Nova Pro - most capable)
#                       us.anthropic.claude-3-haiku-20240307-v1:0  (Claude Haiku)
#   AWS_REGION      - AWS region for Bedrock (uses default if not set)
#   COMMIT_DEBUG    - Set to 1 to enable debug output to stderr

set -euo pipefail

MODEL_ID="${COMMIT_MODEL_ID:-us.amazon.nova-lite-v1:0}"
DEBUG="${COMMIT_DEBUG:-0}"

debug() {
    if [ "$DEBUG" = "1" ]; then
        echo "[DEBUG] $*" >&2
    fi
}

# Collect diff using multiple strategies (in priority order)
# After "git add .", staged changes show in "git diff --cached" or "git diff HEAD"
DIFF_STAT=""
DIFF_CONTENT=""

# Strategy 1: Staged changes (git diff --cached)
DIFF_STAT=$(git diff --cached --stat 2>/dev/null || true)
DIFF_CONTENT=$(git diff --cached 2>/dev/null || true)

# Strategy 2: If no staged changes, try diff against HEAD (catches both staged + unstaged)
if [ -z "$DIFF_STAT" ]; then
    debug "No staged diff found, trying HEAD..."
    DIFF_STAT=$(git diff HEAD --stat 2>/dev/null || true)
    DIFF_CONTENT=$(git diff HEAD 2>/dev/null || true)
fi

# Strategy 3: Unstaged changes
if [ -z "$DIFF_STAT" ]; then
    debug "No HEAD diff found, trying unstaged..."
    DIFF_STAT=$(git diff --stat 2>/dev/null || true)
    DIFF_CONTENT=$(git diff 2>/dev/null || true)
fi

# Strategy 4: Last commit diff (if everything is already committed)
if [ -z "$DIFF_STAT" ]; then
    debug "No working tree diff, using last commit..."
    DIFF_STAT=$(git diff HEAD~1 --stat 2>/dev/null || echo "no diff available")
    DIFF_CONTENT=$(git diff HEAD~1 2>/dev/null || echo "")
fi

debug "Diff stat length: ${#DIFF_STAT}"
debug "Diff content length: ${#DIFF_CONTENT}"

# Truncate diff content to ~6000 chars to stay within token limits while providing good context
DIFF_TRUNCATED="${DIFF_CONTENT:0:6000}"

# Build the prompt
PROMPT="You are a Git commit message expert. Analyze the following git changes and generate a clear, informative commit message.

Rules:
1. Use conventional commit format: type(scope): description
2. Types: feat, fix, docs, refactor, chore, test, style, build, ci, perf
3. The scope should identify the component or area changed (e.g., makefile, config, api, ui)
4. The description should concisely summarize WHAT changed and WHY
5. Return ONLY the commit message on a single line
6. Do NOT wrap in quotes, do NOT add any explanation or preamble
7. Keep it under 100 characters

Files changed:
${DIFF_STAT}

Diff details:
${DIFF_TRUNCATED}"

# Escape the prompt for JSON using jq
ESCAPED_PROMPT=$(echo "$PROMPT" | jq -Rs .)

# Build the messages JSON
MESSAGES="[{\"role\":\"user\",\"content\":[{\"text\":${ESCAPED_PROMPT}}]}]"

debug "Calling Bedrock with model: $MODEL_ID"

# Call Bedrock converse API - capture both stdout and stderr separately
BEDROCK_OUTPUT=""
BEDROCK_ERR=""
BEDROCK_OUTPUT=$(aws bedrock-runtime converse \
    --model-id "$MODEL_ID" \
    --messages "$MESSAGES" \
    --inference-config '{"maxTokens":100,"temperature":0.3}' \
    --query 'output.message.content[0].text' \
    --output text 2>/tmp/bedrock_commit_err) || true

BEDROCK_ERR=$(cat /tmp/bedrock_commit_err 2>/dev/null || true)
rm -f /tmp/bedrock_commit_err

debug "Bedrock output: '$BEDROCK_OUTPUT'"
debug "Bedrock stderr: '$BEDROCK_ERR'"

# Check if we got a valid response
if [ -z "$BEDROCK_OUTPUT" ] || [ "$BEDROCK_OUTPUT" = "None" ] || [ "$BEDROCK_OUTPUT" = "null" ]; then
    debug "Bedrock returned empty/None, using fallback"
    if [ -n "$BEDROCK_ERR" ]; then
        debug "Bedrock error: $BEDROCK_ERR"
    fi
    # Generate a descriptive fallback from the diff stat
    SUMMARY=$(echo "$DIFF_STAT" | head -5 | tr '\n' ', ' | sed 's/, $//')
    echo "chore: update files - ${SUMMARY}"
    exit 0
fi

# Clean up - strip code blocks, remove surrounding quotes, trim whitespace
# Models sometimes wrap output in ```bash ... ``` or ```...```
COMMIT_MSG=$(echo "$BEDROCK_OUTPUT" | \
    sed '/^```/d' | \
    sed 's/^["'"'"']*//;s/["'"'"']*$//' | \
    grep -v '^[[:space:]]*$' | \
    head -1 | \
    xargs)

debug "Final commit message: '$COMMIT_MSG'"

echo "$COMMIT_MSG"
