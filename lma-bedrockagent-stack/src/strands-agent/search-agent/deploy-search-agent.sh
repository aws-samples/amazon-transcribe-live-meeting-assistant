#!/bin/bash

echo "🚀 Starting Search Agent Deployment to Amazon Bedrock AgentCore"
echo "=========================================================="

# Error handling
set -e

# Function to display error messages
error() {
    echo "❌ Error: $1" >&2
    exit 1
}

# Function to backup configuration
backup_config() {
    if [ -d ".agentcore" ]; then
        backup_dir=".agentcore_backup_$(date +%Y%m%d_%H%M%S)"
        echo "📦 Backing up existing configuration to $backup_dir..."
        cp -r .agentcore "$backup_dir"
        echo "✅ Configuration backed up"
    fi
}

# Function to restore configuration
restore_config() {
    local latest_backup
    latest_backup=$(ls -td .agentcore_backup_* | head -n 1)
    if [ -n "$latest_backup" ]; then
        echo "🔄 Restoring configuration from $latest_backup..."
        rm -rf .agentcore
        cp -r "$latest_backup" .agentcore
        echo "✅ Configuration restored"
    else
        error "No backup configuration found"
    fi
}

# Function to cleanup resources
cleanup() {
    echo "🧹 Cleaning up resources..."
    
    # Remove temporary files
    rm -f ./*.log
    rm -f ./*.tmp
    
    # Remove old backups (keep last 5)
    ls -td .agentcore_backup_* 2>/dev/null | tail -n +6 | xargs -r rm -rf
    
    echo "✅ Cleanup completed"
}

# Trap cleanup on script exit
trap cleanup EXIT

# Set and validate AWS configuration
export AWS_PROFILE=default
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1

echo "🔍 Validating AWS configuration..."
echo "✅ Using AWS profile: $AWS_PROFILE"
echo "✅ Using AWS region: $AWS_REGION"

# Validate AWS region
if ! aws ec2 describe-regions --region "$AWS_REGION" --query "Regions[?RegionName=='$AWS_REGION']" --output text > /dev/null 2>&1; then
    error "Invalid AWS region: $AWS_REGION"
fi

# Check if region is supported for Bedrock AgentCore
supported_regions=("us-east-1" "us-west-2" "eu-central-1" "ap-southeast-2")
region_supported=false
for region in "${supported_regions[@]}"; do
    if [ "$AWS_REGION" == "$region" ]; then
        region_supported=true
        break
    fi
done

if [ "$region_supported" = false ]; then
    error "Bedrock AgentCore is not supported in region $AWS_REGION.
Supported regions are:
- US East (N. Virginia): us-east-1
- US West (Oregon): us-west-2
- Europe (Frankfurt): eu-central-1
- Asia Pacific (Sydney): ap-southeast-2"
fi

# Check and install AWS CLI v2
echo "🔍 Checking AWS CLI version..."
if aws --version 2>&1 | grep -q "aws-cli/1"; then
    echo "❌ AWS CLI v1 detected. Installing AWS CLI v2..."
    
    # Create temporary directory for AWS CLI v2 installation
    temp_dir=$(mktemp -d)
    cd "$temp_dir"
    
    # Download and install AWS CLI v2 based on OS
    case "$OSTYPE" in
        darwin*)
            # macOS
            if [[ "$(uname -m)" == "arm64" ]]; then
                # Apple Silicon (M1/M2)
                curl "https://awscli.amazonaws.com/AWSCLIV2-arm64.pkg" -o "AWSCLIV2.pkg"
            else
                # Intel
                curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "AWSCLIV2.pkg"
            fi
            sudo installer -pkg AWSCLIV2.pkg -target /
            ;;
        linux*)
            # Linux
            if command -v apt-get &> /dev/null; then
                # Debian/Ubuntu
                sudo apt-get update
                sudo apt-get install -y unzip
            elif command -v yum &> /dev/null; then
                # RHEL/CentOS/Amazon Linux
                sudo yum install -y unzip
            fi
            
            # Check architecture
            if [[ "$(uname -m)" == "aarch64" ]]; then
                # ARM64
                curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip"
            else
                # x86_64
                curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
            fi
            unzip awscliv2.zip
            sudo ./aws/install --update
            ;;
        *)
            error "Unsupported operating system: $OSTYPE"
            ;;
    esac
    
    # Clean up
    cd - > /dev/null
    rm -rf "$temp_dir"
    
    # Verify installation
    if ! aws --version 2>&1 | grep -q "aws-cli/2"; then
        error "Failed to install AWS CLI v2. Please install manually:
        macOS: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2-mac.html
        Linux: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2-linux.html"
    fi
fi

# Verify AWS CLI v2 version
aws_version=$(aws --version 2>&1 | grep -o "aws-cli/2[^ ]*" | cut -d'/' -f2)
required_version="2.13.0"
if [ "$(printf '%s\n' "$required_version" "$aws_version" | sort -V | head -n1)" = "$required_version" ]; then
    echo "✅ AWS CLI version $aws_version is compatible"
else
    error "AWS CLI version $aws_version is too old. Please upgrade to version $required_version or later"
fi

# Update AWS SDK dependencies
echo "🔄 Updating AWS SDK dependencies..."
pip install --upgrade boto3 botocore

# Enable preview features for Bedrock AgentCore
echo "🔧 Enabling Bedrock AgentCore preview features..."
aws configure set preview.bedrock-agentcore true
aws configure set preview.bedrock-agent true

# Verify Bedrock AgentCore access
echo "🔍 Verifying Bedrock AgentCore access..."
if ! aws bedrock-agentcore-control list-agent-runtimes > /dev/null 2>&1; then
    error "Unable to access Bedrock AgentCore. Please ensure:
1. You have enabled the preview features
2. Your AWS credentials have sufficient permissions
3. You have accepted the preview terms for Bedrock AgentCore
4. You have the latest AWS CLI version
Visit: https://console.aws.amazon.com/bedrock to get started"
fi
echo "✅ Bedrock AgentCore access verified"

# Install Python dependencies
echo "🔄 Installing Python dependencies..."

# Install system dependencies first
case "$OSTYPE" in
    darwin*)
        # macOS
        if ! command -v brew &> /dev/null; then
            error "Homebrew is required. Please install from https://brew.sh"
        fi
        brew install cmake ninja
        ;;
    linux*)
        if command -v apt-get &> /dev/null; then
            # Debian/Ubuntu
            sudo apt-get update
            sudo apt-get install -y cmake ninja-build python3-dev
        elif command -v yum &> /dev/null; then
            # RHEL/CentOS/Amazon Linux
            sudo yum install -y cmake ninja-build python3-devel
        else
            error "Unsupported Linux distribution. Please install cmake, ninja-build, and python3-dev manually."
        fi
        ;;
    *)
        error "Unsupported operating system: $OSTYPE"
        ;;
esac

# Install Python packages
pip install --no-cache-dir -r requirements.txt

# Verify AWS credentials
echo "🔍 Verifying AWS credentials..."
aws sts get-caller-identity > /dev/null
echo "✅ AWS credentials verified"

# Check if required tools are installed
echo "🔍 Checking required tools..."

if ! command -v agentcore &> /dev/null; then
    echo "❌ AgentCore CLI not found. Installing..."
    pip install --upgrade bedrock-agentcore-starter-toolkit mcp
    echo "✅ AgentCore CLI and dependencies installed"
else
    echo "✅ AgentCore CLI found"
    echo "🔄 Updating AgentCore dependencies..."
    pip install --upgrade bedrock-agentcore-starter-toolkit mcp
    echo "✅ Dependencies updated"
fi

if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found. Please install Docker and try again."
    exit 1
else
    echo "✅ Docker found"
fi

if ! command -v jq &> /dev/null; then
    echo "❌ jq not found. Please install jq and try again."
    echo "   macOS: brew install jq"
    echo "   Ubuntu: sudo apt-get install jq"
    exit 1
else
    echo "✅ jq found"
fi

# Check required files
echo "🔍 Checking required files..."
required_files=("search_agent.py" "requirements.txt" "Dockerfile" ".dockerignore")
for file in "${required_files[@]}"; do
    if [ ! -f "$file" ]; then
        echo "❌ Required file not found: $file"
        echo "Please ensure all required files are present in the search-agent directory."
        exit 1
    fi
done
echo "✅ All required files found"

# Get DuckDuckGo MCP ARN
echo "🔍 Getting DuckDuckGo MCP ARN..."
DUCKDUCKGO_MCP_ARN=$(aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?contains(name, 'server')].agentRuntimeArn" --output text)
if [ -z "$DUCKDUCKGO_MCP_ARN" ]; then
    echo "❌ DuckDuckGo MCP server not found. Please deploy the DuckDuckGo MCP server first."
    exit 1
fi
echo "✅ Found DuckDuckGo MCP ARN: $DUCKDUCKGO_MCP_ARN"

# Step 1: Configure AgentCore
echo ""
echo "📋 Step 1: Configuring agent for deployment..."

# Check if already configured
if [ -f ".agentcore/config.json" ]; then
    echo "🔍 Found existing AgentCore configuration."
    echo "Options:"
    echo "1. Use existing configuration"
    echo "2. Create new configuration"
    echo "3. Restore from backup"
    read -r -p "Choose an option (1-3): " config_option
    
    case $config_option in
        1)
            echo "✅ Using existing configuration"
            ;;
        2)
            echo "🔄 Creating new configuration..."
            backup_config
            rm -rf .agentcore
            ;;
        3)
            restore_config
            ;;
        *)
            error "Invalid option selected"
            ;;
    esac
fi

if [ ! -f ".agentcore/config.json" ]; then
    echo "🔧 Running AgentCore configuration..."
    echo "📝 You will be prompted for:"
    echo "   - Execution Role: Press Enter to auto-create"
    echo "   - ECR Repository: Press Enter to auto-create (will create 'bedrock_agentcore-search_agent')"
    echo "   - OAuth: Type 'no' (simplified deployment)"
    echo ""
    echo "Press Enter to continue..."
    read -r
    
    # Configure with environment variables for DuckDuckGo MCP
    export DUCKDUCKGO_MCP_ARN
    agentcore configure -e search_agent.py \
        --env DUCKDUCKGO_MCP_ARN="$DUCKDUCKGO_MCP_ARN" \
        --env AWS_REGION="us-east-1" \
        --env AWS_DEFAULT_REGION="us-east-1" \
        --env AGENTCORE_RUNTIME="true"
    echo "✅ AgentCore configuration completed"
fi

# Step 2: Build and Deploy
echo ""
echo "📋 Step 2: Building and deploying to AWS..."

# Build Docker image
echo "🔨 Building Docker image..."
if ! docker build \
    --build-arg AWS_REGION="$AWS_REGION" \
    --build-arg DUCKDUCKGO_MCP_ARN="$DUCKDUCKGO_MCP_ARN" \
    -t bedrock_agentcore-search_agent . ; then
    error "Docker build failed. Check the build output above for details."
fi
echo "✅ Docker build completed"

# Test the Docker image
echo "🧪 Testing Docker image..."
if ! docker run --rm bedrock_agentcore-search_agent python -c "import search_agent; print('Image test successful')" ; then
    error "Docker image test failed. The image may be corrupted."
fi
echo "✅ Docker image test passed"

# Deploy to AgentCore
echo "🚀 Launching deployment (this may take several minutes)..."
if ! agentcore launch ; then
    error "Deployment failed. Check the AgentCore logs for details:
    1. Open AWS CloudWatch console
    2. Navigate to Log Groups
    3. Check /aws/bedrock-agentcore/search_agent"
fi

echo ""
echo "✅ Deployment completed!"

# Step 3: Extract and save the Agent ARN
echo ""
echo "📋 Step 3: Extracting deployment information..."

# The ARN should be in the output, but let's also check the config
if [ -f ".agentcore/config.json" ]; then
    AGENT_ARN=$(jq -r '.agent_runtime_arn // empty' .agentcore/config.json)
    if [ -n "$AGENT_ARN" ] && [ "$AGENT_ARN" != "null" ]; then
        echo "✅ Agent Runtime ARN: $AGENT_ARN"
        
        # Save to environment file
        cat > config.env << EOF
# Search Agent Configuration
export AGENT_ARN=$AGENT_ARN
export AWS_PROFILE=default
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AGENTCORE_RUNTIME=true
export DUCKDUCKGO_MCP_ARN=$DUCKDUCKGO_MCP_ARN
EOF
        echo "✅ Environment configuration saved to config.env"
    else
        echo "⚠️  Could not extract Agent ARN from config. Please check the deployment output above."
    fi
fi

# Final instructions
echo ""
echo "🎉 Deployment Complete!"
echo "======================"
echo ""
echo "📋 Next Steps:"
echo "1. Load environment variables:"
echo "   source config.env"
echo ""
echo "2. Test your deployment:"
echo "   python test_search_agent.py"
echo ""
echo "3. Use MCP Inspector for interactive testing:"
echo "   npx @modelcontextprotocol/inspector"
echo ""
echo "📁 Files created:"
echo "   - config.env (Deployment environment)"
echo "   - .agentcore/config.json (AgentCore configuration)"
echo ""
echo "🤖 Your Search Agent is now deployed and ready to use!"

if [ -n "$AGENT_ARN" ]; then
    echo "📋 Agent Runtime ARN: $AGENT_ARN"
fi
