#!/bin/bash
# Test script for VNC Lambda WebSocket proxy

set -e

LAMBDA_NAME="LMA-dev-stack-vnc-proxy"
API_ID="wsdj4af28j"
REGION="us-east-1"
VP_ID="${1:-test-vp-id}"

echo "=== Testing VNC Lambda WebSocket Proxy ==="
echo "Lambda: $LAMBDA_NAME"
echo "API Gateway: $API_ID"
echo "VP ID: $VP_ID"
echo ""

# Test 1: Invoke Lambda directly with $connect event
echo "Test 1: Direct Lambda invocation ($connect)"
cat > /tmp/connect-event.json << EOF
{
  "requestContext": {
    "routeKey": "\$connect",
    "connectionId": "test-connection-123",
    "domainName": "${API_ID}.execute-api.${REGION}.amazonaws.com",
    "stage": "prod"
  },
  "queryStringParameters": {
    "vpId": "${VP_ID}"
  }
}
EOF

echo "Invoking Lambda..."
aws lambda invoke \
  --function-name $LAMBDA_NAME \
  --cli-binary-format raw-in-base64-out \
  --payload file:///tmp/connect-event.json \
  /tmp/lambda-response.json

echo ""
echo "Lambda Response:"
cat /tmp/lambda-response.json
echo ""
echo ""

# Test 2: Check Lambda logs
echo "Test 2: Checking Lambda CloudWatch logs..."
LOG_GROUP="/aws/lambda/$LAMBDA_NAME"

if aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'logGroups[0].logGroupName' --output text 2>/dev/null | grep -q "$LOG_GROUP"; then
  echo "Log group exists. Recent logs:"
  aws logs tail "$LOG_GROUP" --since 1m --format short 2>/dev/null || echo "No recent logs"
else
  echo "Log group does not exist yet (Lambda hasn't been invoked)"
fi

echo ""
echo ""

# Test 3: Test WebSocket connection using wscat (if installed)
if command -v wscat &> /dev/null; then
  echo "Test 3: Testing WebSocket connection with wscat"
  WS_URL="wss://${API_ID}.execute-api.${REGION}.amazonaws.com/prod?vpId=${VP_ID}"
  echo "Connecting to: $WS_URL"
  echo "Press Ctrl+C to exit"
  timeout 5 wscat -c "$WS_URL" || echo "Connection test completed (timeout or error)"
else
  echo "Test 3: Skipped (wscat not installed)"
  echo "To install: npm install -g wscat"
fi

echo ""
echo "=== Test Complete ==="
