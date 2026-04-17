#!/bin/bash
# End-to-end integration test for MCP API Key endpoint.
# Tests the full MCP JSON-RPC 2.0 protocol over REST API Gateway with API key auth.
#
# Usage:
#   ./utilities/test-mcp-api-key.sh <api-key> [api-gateway-url]
#
# If api-gateway-url is omitted, it is read from the LMA CloudFormation stack output
# (MCPServerApiKeyEndpoint).
#
# Prerequisites:
#   - A deployed LMA stack with MCP enabled
#   - A valid API key generated from the LMA UI
#   - curl and jq installed

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

API_KEY="${1:-}"
API_URL="${2:-}"
STACK_NAME="${STACK_NAME:-LMA}"
REGION="${AWS_REGION:-us-east-1}"

if [ -z "$API_KEY" ]; then
    echo "Usage: $0 <api-key> [api-gateway-url]"
    echo ""
    echo "  api-key:          Your LMA MCP API key (e.g., lma_xxxxxxxx-...)"
    echo "  api-gateway-url:  Optional. MCPServerApiKeyEndpoint URL."
    echo "                    Auto-detected from CloudFormation if omitted."
    exit 1
fi

# Auto-detect API URL from CloudFormation output if not provided
if [ -z "$API_URL" ]; then
    echo "Auto-detecting API URL from CloudFormation stack '${STACK_NAME}'..."
    API_URL=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='MCPServerApiKeyEndpoint'].OutputValue" \
        --output text 2>/dev/null || true)

    if [ -z "$API_URL" ] || [ "$API_URL" = "None" ]; then
        echo -e "${RED}ERROR: Could not find MCPServerApiKeyEndpoint output.${NC}"
        echo "Provide the URL as the second argument."
        exit 1
    fi
    echo "  Found: $API_URL"
fi

PASS=0
FAIL=0

run_test() {
    local test_name="$1"
    local method="$2"
    local auth_header="$3"
    local body="$4"
    local expected_check="$5"

    echo -n "  Testing: $test_name... "

    RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -H "$auth_header" \
        -d "$body" 2>&1)

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if eval "$expected_check"; then
        echo -e "${GREEN}PASS${NC}"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "    HTTP: $HTTP_CODE"
        echo "    Body: $BODY"
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "========================================="
echo " MCP API Key Endpoint Integration Tests"
echo "========================================="
echo "URL: $API_URL"
echo "Key: ${API_KEY:0:12}..."
echo ""

# Test 1: Initialize with Bearer token
echo "--- MCP Protocol Tests ---"
run_test "initialize (Bearer)" \
    "POST" \
    "Authorization: Bearer $API_KEY" \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
    '[ "$HTTP_CODE" = "200" ] && echo "$BODY" | jq -e ".result.serverInfo.name" > /dev/null 2>&1'

# Test 2: Initialize with x-api-key header
run_test "initialize (x-api-key)" \
    "POST" \
    "x-api-key: $API_KEY" \
    '{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}}' \
    '[ "$HTTP_CODE" = "200" ] && echo "$BODY" | jq -e ".result.protocolVersion" > /dev/null 2>&1'

# Test 3: tools/list
run_test "tools/list" \
    "POST" \
    "Authorization: Bearer $API_KEY" \
    '{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}' \
    '[ "$HTTP_CODE" = "200" ] && [ "$(echo "$BODY" | jq ".result.tools | length")" = "6" ]'

# Test 4: tools/call - list_meetings
run_test "tools/call (list_meetings)" \
    "POST" \
    "Authorization: Bearer $API_KEY" \
    '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_meetings","arguments":{"limit":2}}}' \
    '[ "$HTTP_CODE" = "200" ] && echo "$BODY" | jq -e ".result.content" > /dev/null 2>&1'

# Test 5: ping
run_test "ping" \
    "POST" \
    "Authorization: Bearer $API_KEY" \
    '{"jsonrpc":"2.0","id":5,"method":"ping"}' \
    '[ "$HTTP_CODE" = "200" ] && echo "$BODY" | jq -e ".result" > /dev/null 2>&1'

# Test 6: Bad key should be rejected
echo ""
echo "--- Auth Rejection Tests ---"
run_test "bad key rejected" \
    "POST" \
    "Authorization: Bearer lma_invalid-key-should-fail" \
    '{"jsonrpc":"2.0","id":6,"method":"ping"}' \
    '[ "$HTTP_CODE" = "403" ] || [ "$HTTP_CODE" = "401" ]'

# Test 7: No auth header should be rejected
run_test "no auth rejected" \
    "POST" \
    "X-Nothing: true" \
    '{"jsonrpc":"2.0","id":7,"method":"ping"}' \
    '[ "$HTTP_CODE" = "403" ] || [ "$HTTP_CODE" = "401" ]'

# Summary
echo ""
echo "========================================="
echo -e " Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
echo "========================================="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
