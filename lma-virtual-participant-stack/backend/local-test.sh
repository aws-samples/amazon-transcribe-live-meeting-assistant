#!/bin/bash

# Local VP Testing Script for LMA
# This script fetches required environment variables from your CloudFormation stack
# and runs the Virtual Participant container locally for debugging.
#
# Usage:
#   ./local-test.sh <STACK_NAME> <MEETING_PLATFORM> <MEETING_ID> [MEETING_PASSWORD]
#
# Example:
#   ./local-test.sh LMA-dev-stack-2 WEBEX 25523622514
#   ./local-test.sh LMA-dev-stack-2 ZOOM 123456789 mypassword

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check arguments
if [ $# -lt 3 ]; then
    echo -e "${RED}Usage: $0 <STACK_NAME> <MEETING_PLATFORM> <MEETING_ID> [MEETING_PASSWORD]${NC}"
    echo ""
    echo "Arguments:"
    echo "  STACK_NAME        - Your LMA CloudFormation stack name (e.g., LMA-dev-stack-2)"
    echo "  MEETING_PLATFORM  - WEBEX, ZOOM, TEAMS, or CHIME"
    echo "  MEETING_ID        - The meeting ID to join"
    echo "  MEETING_PASSWORD  - Optional meeting password"
    echo ""
    echo "Example:"
    echo "  $0 LMA-dev-stack-2 WEBEX 25523622514"
    exit 1
fi

STACK_NAME=$1
MEETING_PLATFORM=$2
MEETING_ID=$3
MEETING_PASSWORD=${4:-""}

echo -e "${GREEN}=== LMA Virtual Participant Local Test Setup ===${NC}"
echo ""
echo "Stack Name: $STACK_NAME"
echo "Meeting Platform: $MEETING_PLATFORM"
echo "Meeting ID: $MEETING_ID"
echo ""

# Get AWS region
AWS_REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
echo "AWS Region: $AWS_REGION"
echo ""

echo -e "${YELLOW}Fetching CloudFormation resources...${NC}"

# Get resources from the main stack (Kinesis stream and S3 bucket are here)
CALL_DATA_STREAM_NAME=$(aws cloudformation list-stack-resources \
    --stack-name "$STACK_NAME" \
    --query "StackResourceSummaries[?LogicalResourceId=='CallDataStream'].PhysicalResourceId" \
    --output text 2>/dev/null)
echo "Call Data Stream: $CALL_DATA_STREAM_NAME"

RECORDINGS_BUCKET_NAME=$(aws cloudformation list-stack-resources \
    --stack-name "$STACK_NAME" \
    --query "StackResourceSummaries[?LogicalResourceId=='RecordingsBucket'].PhysicalResourceId" \
    --output text 2>/dev/null)
echo "Recordings Bucket: $RECORDINGS_BUCKET_NAME"

# Find the AI stack (nested)
AI_STACK=$(aws cloudformation list-stack-resources \
    --stack-name "$STACK_NAME" \
    --query "StackResourceSummaries[?LogicalResourceId=='AISTACK'].PhysicalResourceId" \
    --output text 2>/dev/null)
echo "AI Stack: $AI_STACK"

# Get the VP Task Registry table from AI stack
if [ -n "$AI_STACK" ]; then
    VP_TASK_REGISTRY_TABLE_NAME=$(aws cloudformation list-stack-resources \
        --stack-name "$AI_STACK" \
        --query "StackResourceSummaries[?LogicalResourceId=='VPTaskRegistry'].PhysicalResourceId" \
        --output text 2>/dev/null)
    echo "VP Task Registry Table: $VP_TASK_REGISTRY_TABLE_NAME"
    
    # Get AppSync API ID from AI stack
    APPSYNC_API_ARN=$(aws cloudformation list-stack-resources \
        --stack-name "$AI_STACK" \
        --query "StackResourceSummaries[?LogicalResourceId=='AppSyncApiEncrypted'].PhysicalResourceId" \
        --output text 2>/dev/null)
    
    # Extract API ID from ARN (format: arn:aws:appsync:region:account:apis/API_ID)
    APPSYNC_API_ID=$(echo "$APPSYNC_API_ARN" | sed 's/.*apis\///')
    
    if [ -n "$APPSYNC_API_ID" ]; then
        GRAPHQL_ENDPOINT=$(aws appsync get-graphql-api --api-id "$APPSYNC_API_ID" --query "graphqlApi.uris.GRAPHQL" --output text 2>/dev/null)
        EVENTS_API_ENDPOINT=$(aws appsync get-graphql-api --api-id "$APPSYNC_API_ID" --query "graphqlApi.uris.REALTIME" --output text 2>/dev/null)
    fi
fi
echo "GraphQL Endpoint: $GRAPHQL_ENDPOINT"
echo "Events API Endpoint: $EVENTS_API_ENDPOINT"

# Validate required values
echo ""
if [ -z "$CALL_DATA_STREAM_NAME" ] || [ -z "$RECORDINGS_BUCKET_NAME" ] || [ -z "$GRAPHQL_ENDPOINT" ]; then
    echo -e "${RED}ERROR: Could not fetch all required CloudFormation resources.${NC}"
    echo "Please verify your stack name and ensure the stack is fully deployed."
    echo ""
    echo "Missing values:"
    [ -z "$CALL_DATA_STREAM_NAME" ] && echo "  - CALL_DATA_STREAM_NAME"
    [ -z "$RECORDINGS_BUCKET_NAME" ] && echo "  - RECORDINGS_BUCKET_NAME"
    [ -z "$GRAPHQL_ENDPOINT" ] && echo "  - GRAPHQL_ENDPOINT"
    exit 1
fi

# Generate a unique meeting name and VP ID
MEETING_NAME="LocalTest-$(date +%m%d%y_%H%M)"
VIRTUAL_PARTICIPANT_ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)
LMA_USER=$(aws sts get-caller-identity --query "Arn" --output text 2>/dev/null | sed 's/.*\///' || echo "local-tester")

echo ""
echo -e "${GREEN}=== Environment Configuration ===${NC}"
echo ""

# Create the env file
ENV_FILE="$(dirname "$0")/.env.local"
cat > "$ENV_FILE" << EOF
# LMA Virtual Participant Local Test Environment
# Generated on $(date)
# Stack: $STACK_NAME

# LOCAL TEST MODE - Skip ALB registration and AppSync updates
LOCAL_TEST=true

# Meeting Configuration
MEETING_PLATFORM=$MEETING_PLATFORM
MEETING_ID=$MEETING_ID
MEETING_PASSWORD=$MEETING_PASSWORD
MEETING_NAME=$MEETING_NAME
MEETING_TIME=$(date +%s)
LMA_USER=$LMA_USER
VIRTUAL_PARTICIPANT_ID=$VIRTUAL_PARTICIPANT_ID

# LMA Integration
CALL_DATA_STREAM_NAME=$CALL_DATA_STREAM_NAME
RECORDINGS_BUCKET_NAME=$RECORDINGS_BUCKET_NAME
RECORDINGS_KEY_PREFIX=lma-audio-recordings/
GRAPHQL_ENDPOINT=$GRAPHQL_ENDPOINT
VP_TASK_REGISTRY_TABLE_NAME=$VP_TASK_REGISTRY_TABLE_NAME
EVENTS_API_ENDPOINT=$EVENTS_API_ENDPOINT

# AWS Configuration
AWS_REGION=$AWS_REGION
AWS_DEFAULT_REGION=$AWS_REGION

# Transcription Configuration
TRANSCRIBE_LANGUAGE_CODE=en-US
ENABLE_CONTENT_REDACTION=false
ENABLE_AUDIO_RECORDING=true

# Display Configuration (for local testing)
DISPLAY=:99
PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
EOF

echo "Environment file created: $ENV_FILE"
echo ""
cat "$ENV_FILE"
echo ""

# Check if Docker is available
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}Docker not found. You can run the container manually with:${NC}"
    echo ""
    echo "docker build -t lma-vp-local ."
    echo "docker run -it --rm \\"
    echo "  --env-file $ENV_FILE \\"
    echo "  -p 5900:5900 \\"
    echo "  -p 5901:5901 \\"
    echo "  -v ~/.aws:/home/appuser/.aws:ro \\"
    echo "  lma-vp-local"
    exit 0
fi

echo -e "${GREEN}=== Building Docker Image ===${NC}"
cd "$(dirname "$0")"
docker build -t lma-vp-local .

echo ""
echo -e "${GREEN}=== Starting Virtual Participant Container ===${NC}"
echo ""
echo "VNC will be available at:"
echo "  - VNC Client: localhost:5900"
echo "  - Web Browser (noVNC): http://localhost:5901/vnc.html"
echo ""
echo "Press Ctrl+C to stop the container."
echo ""

# Run the container with AWS credentials mounted
docker run -it --rm \
    --name lma-vp-local-test \
    --env-file "$ENV_FILE" \
    -p 5900:5900 \
    -p 5901:5901 \
    -v ~/.aws:/home/appuser/.aws:ro \
    lma-vp-local
