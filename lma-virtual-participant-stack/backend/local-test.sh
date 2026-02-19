#!/bin/bash

# Local VP Testing Script for LMA
# This script fetches required environment variables from your CloudFormation stack
# and runs the Virtual Participant container locally for debugging.
#
# Usage:
#   ./local-test.sh [OPTIONS] <STACK_NAME> <MEETING_PLATFORM> <MEETING_ID> [MEETING_PASSWORD]
#
# Options:
#   --dev             Enable development mode with auto-reload on file changes
#   --reuse-env       Reuse existing .env.local file (skip CloudFormation fetch)
#
# Example:
#   ./local-test.sh LMA-dev-stack-2 WEBEX 25523622514
#   ./local-test.sh --dev LMA-dev-stack-2 ZOOM 123456789 mypassword
#   ./local-test.sh --reuse-env LMA-dev-stack-2 WEBEX 25523622514

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse flags
DEV_MODE=false
REUSE_ENV=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --dev)
            DEV_MODE=true
            shift
            ;;
        --reuse-env)
            REUSE_ENV=true
            shift
            ;;
        *)
            break
            ;;
    esac
done

# Check arguments
if [ $# -lt 3 ]; then
    echo -e "${RED}Usage: $0 [OPTIONS] <STACK_NAME> <MEETING_PLATFORM> <MEETING_ID> [MEETING_PASSWORD]${NC}"
    echo ""
    echo "Options:"
    echo "  --dev             Enable development mode with auto-reload on file changes"
    echo "  --reuse-env       Reuse existing .env.local file (skip CloudFormation fetch)"
    echo ""
    echo "Arguments:"
    echo "  STACK_NAME        - Your LMA CloudFormation stack name (e.g., LMA-dev-stack-2)"
    echo "  MEETING_PLATFORM  - WEBEX, ZOOM, TEAMS, or CHIME"
    echo "  MEETING_ID        - The meeting ID to join"
    echo "  MEETING_PASSWORD  - Optional meeting password"
    echo ""
    echo "Example:"
    echo "  $0 LMA-dev-stack-2 WEBEX 25523622514"
    echo "  $0 --dev LMA-dev-stack-2 WEBEX 25523622514"
    exit 1
fi

STACK_NAME=$1
MEETING_PLATFORM=$2
MEETING_ID=$3
MEETING_PASSWORD=${4:-""}

echo ""
echo -e "${GREEN}=== LMA Virtual Participant Local Test Setup ===${NC}"
echo ""
if [ "$DEV_MODE" = true ]; then
    echo -e "${BLUE}🔧 Development Mode: ENABLED${NC}"
    echo "   - Source directory will be mounted as volume"
    echo "   - Auto-reload on TypeScript file changes"
    echo "   - Container will persist (use 'docker stop lma-vp-local-test' to stop)"
    echo ""
fi
echo "Stack Name: $STACK_NAME"
echo "Meeting Platform: $MEETING_PLATFORM"
echo "Meeting ID: $MEETING_ID"
echo ""

# Get AWS region
AWS_REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
echo "AWS Region: $AWS_REGION"
echo ""

# Check if reusing existing env file
ENV_FILE="$(dirname "$0")/.env.local"
if [ "$REUSE_ENV" = true ]; then
    if [ ! -f "$ENV_FILE" ]; then
        echo -e "${RED}ERROR: .env.local file not found. Cannot reuse environment.${NC}"
        echo "Run without --reuse-env flag to generate a new environment file."
        exit 1
    fi
    
    echo -e "${BLUE}Reusing existing environment file: $ENV_FILE${NC}"
    echo ""
    echo -e "${YELLOW}Updating meeting parameters only...${NC}"
    
    # Update only meeting-specific parameters
    sed -i.bak "s/^MEETING_PLATFORM=.*/MEETING_PLATFORM=$MEETING_PLATFORM/" "$ENV_FILE"
    sed -i.bak "s/^MEETING_ID=.*/MEETING_ID=$MEETING_ID/" "$ENV_FILE"
    sed -i.bak "s/^MEETING_PASSWORD=.*/MEETING_PASSWORD=$MEETING_PASSWORD/" "$ENV_FILE"
    # sed -i.bak "s/^MEETING_NAME=.*/MEETING_NAME=LocalTest-$(date +%m%d%y_%H%M)/" "$ENV_FILE"  # Comment this line if setting in .env.local to a real meeting id so sonic/elevenlabs tool calls work.
    sed -i.bak "s/^MEETING_TIME=.*/MEETING_TIME=$(date +%s)/" "$ENV_FILE"
    sed -i.bak "s/^DEV_MODE=.*/DEV_MODE=$DEV_MODE/" "$ENV_FILE"
    rm -f "$ENV_FILE.bak"
    
    echo "✓ Meeting parameters updated"
    echo ""
    
    # Skip to Docker build
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
    
    # Jump to container run section
    SKIP_ENV_GENERATION=true
else
    SKIP_ENV_GENERATION=false
fi

if [ "$SKIP_ENV_GENERATION" = false ]; then
    echo -e "${YELLOW}Fetching CloudFormation resources...${NC}"

    # Find the VP stack (nested)
    VP_STACK=$(aws cloudformation list-stack-resources \
        --stack-name "$STACK_NAME" \
        --query "StackResourceSummaries[?LogicalResourceId=='VIRTUALPARTICIPANTSTACK'].PhysicalResourceId" \
        --output text 2>/dev/null)
    echo "VP Stack: $VP_STACK"

    # Get Voice Assistant parameters from VP stack
    if [ -n "$VP_STACK" ]; then
        VOICE_ASSISTANT_PROVIDER=$(aws cloudformation describe-stacks \
            --stack-name "$VP_STACK" \
            --query "Stacks[0].Parameters[?ParameterKey=='VoiceAssistantProvider'].ParameterValue" \
            --output text 2>/dev/null || echo "none")
        
        VOICE_ASSISTANT_ACTIVATION_MODE=$(aws cloudformation describe-stacks \
            --stack-name "$VP_STACK" \
            --query "Stacks[0].Parameters[?ParameterKey=='VoiceAssistantActivationMode'].ParameterValue" \
            --output text 2>/dev/null || echo "always_active")
        
        ELEVENLABS_AGENT_ID=$(aws cloudformation describe-stacks \
            --stack-name "$VP_STACK" \
            --query "Stacks[0].Parameters[?ParameterKey=='ElevenLabsAgentId'].ParameterValue" \
            --output text 2>/dev/null || echo "")
        
        NOVA_MODEL_ID=$(aws cloudformation describe-stacks \
            --stack-name "$VP_STACK" \
            --query "Stacks[0].Parameters[?ParameterKey=='NovaModelId'].ParameterValue" \
            --output text 2>/dev/null || echo "amazon.nova-sonic-2")
        
        NOVA_SYSTEM_PROMPT=$(aws cloudformation describe-stacks \
            --stack-name "$VP_STACK" \
            --query "Stacks[0].Parameters[?ParameterKey=='NovaSystemPrompt'].ParameterValue" \
            --output text 2>/dev/null || echo "You are Alex, an AI meeting assistant. Be concise and helpful.")
        
        STRANDS_LAMBDA_ARN=$(aws cloudformation describe-stacks \
            --stack-name "$VP_STACK" \
            --query "Stacks[0].Parameters[?ParameterKey=='StrandsLambdaArn'].ParameterValue" \
            --output text 2>/dev/null || echo "")
        
        echo "Voice Assistant Provider: $VOICE_ASSISTANT_PROVIDER"
        echo "Voice Assistant Activation Mode: $VOICE_ASSISTANT_ACTIVATION_MODE"
        if [ "$VOICE_ASSISTANT_PROVIDER" = "elevenlabs" ]; then
            echo "ElevenLabs Agent ID: $ELEVENLABS_AGENT_ID"
        elif [ "$VOICE_ASSISTANT_PROVIDER" = "aws_nova" ]; then
            echo "Nova Model ID: $NOVA_MODEL_ID"
            echo "Nova System Prompt: ${NOVA_SYSTEM_PROMPT:0:50}..."
            if [ -n "$STRANDS_LAMBDA_ARN" ]; then
                echo "Strands Lambda ARN: ${STRANDS_LAMBDA_ARN:0:80}..."
            fi
        fi
    fi

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
        
        # Get the Event Sourcing table (CallEventTable) from AI stack
        EVENT_SOURCING_TABLE_NAME=$(aws cloudformation describe-stacks \
            --stack-name "$AI_STACK" \
            --query "Stacks[0].Outputs[?OutputKey=='EventSourcingTableName'].OutputValue" \
            --output text 2>/dev/null)
        echo "Event Sourcing Table: $EVENT_SOURCING_TABLE_NAME"
        
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

# Development Mode
DEV_MODE=$DEV_MODE

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
DYNAMODB_TABLE_NAME=$EVENT_SOURCING_TABLE_NAME
EVENTS_API_ENDPOINT=$EVENTS_API_ENDPOINT

# AWS Configuration
AWS_REGION=$AWS_REGION
AWS_DEFAULT_REGION=$AWS_REGION

# Transcription Configuration
TRANSCRIBE_LANGUAGE_CODE=en-US
ENABLE_CONTENT_REDACTION=false
ENABLE_AUDIO_RECORDING=true

# Voice Assistant Configuration
# Values fetched from CloudFormation stack (VP_STACK)
# For ElevenLabs, set API key as environment variable:
#   export ELEVENLABS_API_KEY="your-key"
VOICE_ASSISTANT_PROVIDER=${VOICE_ASSISTANT_PROVIDER:-none}
VOICE_ASSISTANT_ACTIVATION_MODE=${VOICE_ASSISTANT_ACTIVATION_MODE:-always_active}
ELEVENLABS_API_KEY=${ELEVENLABS_API_KEY:-}
ELEVENLABS_AGENT_ID=${ELEVENLABS_AGENT_ID:-}
NOVA_MODEL_ID=${NOVA_MODEL_ID:-amazon.nova-2-sonic-v1:0}
NOVA_SYSTEM_PROMPT=${NOVA_SYSTEM_PROMPT:-You are Alex, an AI meeting assistant. Be concise and helpful.}
STRANDS_LAMBDA_ARN=${STRANDS_LAMBDA_ARN:-}

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
fi

if [ "$DEV_MODE" = true ]; then
    echo -e "${BLUE}Development Mode Commands:${NC}"
    echo "  - View logs:        docker logs -f lma-vp-local-test"
    echo "  - Stop container:   docker stop lma-vp-local-test"
    echo "  - Remove container: docker rm lma-vp-local-test"
    echo "  - Exec into shell:  docker exec -it lma-vp-local-test /bin/bash"
    echo ""
    echo "TypeScript changes in ./src will automatically trigger rebuild and restart."
    echo ""
else
    echo "Press Ctrl+C to stop the container."
    echo ""
fi

# Run the container with AWS credentials mounted
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Check if container already exists
if docker ps -a --format '{{.Names}}' | grep -q "^lma-vp-local-test$"; then
    echo -e "${YELLOW}Container 'lma-vp-local-test' already exists.${NC}"
    if [ "$DEV_MODE" = true ]; then
        echo "Removing existing container..."
        docker rm -f lma-vp-local-test
    else
        echo "Please remove it first with: docker rm -f lma-vp-local-test"
        exit 1
    fi
fi

if [ "$DEV_MODE" = true ]; then
    # Development mode: mount source, keep container running
    docker run -it \
        --name lma-vp-local-test \
        --env-file "$ENV_FILE" \
        --user root \
        -p 5900:5900 \
        -p 5901:5901 \
        -v ~/.aws:/root/.aws:ro \
        -v "$SCRIPT_DIR/src":/srv/src \
        lma-vp-local
else
    # Production mode: no volume mounts, remove on exit
    docker run -it --rm \
        --name lma-vp-local-test \
        --env-file "$ENV_FILE" \
        --user root \
        -p 5900:5900 \
        -p 5901:5901 \
        -v ~/.aws:/root/.aws:ro \
        lma-vp-local
fi
