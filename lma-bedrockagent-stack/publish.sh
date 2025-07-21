#!/bin/bash

##############################################################################################
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
##############################################################################################

##############################################################################################
# Create new Cfn artifacts bucket if not already existing, and publish template and artifacts
# Also deploy Strands Agent and MCP Server to Bedrock AgentCore Runtime
# usage: ./publish.sh <cfn_bucket> <cfn_prefix> <region> [public] [deploy-agentcore]
##############################################################################################
# Stop the publish process on failures
set -e

# use current directory name as template name
NAME=$(basename `pwd`)

USAGE="$0 <cfn_bucket> <cfn_prefix> <region> [public] [deploy-agentcore]"

BUCKET=$1
[ -z "$BUCKET" ] && echo "Cfn bucket name is required parameter. Usage $USAGE" && exit 1

PREFIX=$2
[ -z "$PREFIX" ] && echo "Prefix is required parameter. Usage $USAGE" && exit 1

# Remove trailing slash from prefix if needed
[[ "${PREFIX}" == */ ]] && PREFIX="${PREFIX%?}"

REGION=$3
[ -z "$REGION" ] && echo "Region is a required parameter. Usage $USAGE" && exit 1
export AWS_DEFAULT_REGION=$REGION

ACL=$4
if [ "$ACL" == "public" ]; then
  echo "Published S3 artifacts will be acessible by public (read-only)"
  PUBLIC=true
else
  echo "Published S3 artifacts will NOT be acessible by public."
  PUBLIC=false
fi

DEPLOY_AGENTCORE=$5
if [ "$DEPLOY_AGENTCORE" == "deploy-agentcore" ]; then
  echo "Will deploy AgentCore components after CloudFormation packaging"
  DEPLOY_AGENTCORE_FLAG=true
else
  echo "Skipping AgentCore deployment"
  DEPLOY_AGENTCORE_FLAG=false
fi

# Create bucket if it doesn't already exist
if [ -x $(aws s3api list-buckets --query 'Buckets[].Name' | grep "\"$BUCKET\"") ]; then
  echo "Creating s3 bucket: $BUCKET"
  aws s3 mb s3://${BUCKET} || exit 1
  aws s3api put-bucket-versioning --bucket ${BUCKET} --versioning-configuration Status=Enabled || exit 1
else
  echo "Using existing bucket: $BUCKET"
fi

echo -n "Make temp dir: "
timestamp=$(date "+%Y%m%d_%H%M")
tmpdir=/tmp/$NAME
[ -d $tmpdir ] && rm -fr $tmpdir
mkdir -p $tmpdir

template=template.yaml
s3_template="s3://${BUCKET}/${PREFIX}/${NAME}/template.yaml"
https_template="https://s3.${REGION}.amazonaws.com/${BUCKET}/${PREFIX}/${NAME}/template.yaml"

echo "PACKAGING $NAME"
aws cloudformation package \
--template-file ${template} \
--output-template-file ${tmpdir}/tmp.template.yaml \
--s3-bucket $BUCKET --s3-prefix ${PREFIX}/${NAME} \
--region ${REGION} || exit 1

echo "Inline edit ${tmpdir}/tmp.template.yaml to replace "
echo "   <ARTIFACT_BUCKET_TOKEN> with bucket name: $BUCKET"
echo "   <ARTIFACT_PREFIX_TOKEN> with prefix: $PREFIX"
echo "   <REGION_TOKEN> with region: $REGION"
cat ${tmpdir}/tmp.template.yaml | 
sed -e "s%<ARTIFACT_BUCKET_TOKEN>%$BUCKET%g" | 
sed -e "s%<ARTIFACT_PREFIX_TOKEN>%$PREFIX%g" |
sed -e "s%<REGION_TOKEN>%$REGION%g" > ${tmpdir}/${template}

echo "Uploading template file to: ${s3_template}"
aws s3 cp ${tmpdir}/${template} ${s3_template}
echo "Validating template"
aws cloudformation validate-template --template-url ${https_template} > /dev/null || exit 1
echo "Validated: ${https_template}"

if $PUBLIC; then
  echo "Setting public read ACLs on published artifacts"
  files=$(aws s3api list-objects --bucket ${BUCKET} --prefix ${PREFIX} --query "(Contents)[].[Key]" --output text)
  for file in $files
    do
    aws s3api put-object-acl --acl public-read --bucket ${BUCKET} --key $file
    done
fi

echo Published $NAME - Template URL: $https_template

# Deploy AgentCore components if requested
if $DEPLOY_AGENTCORE_FLAG; then
  echo ""
  echo "Deploying LMA Search Agent and MCP Server to Bedrock AgentCore Runtime"
  echo "============================================================================"

  # Configuration
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

  # Check prerequisites
  echo "Checking prerequisites..."

  if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is required but not installed"
    exit 1
  fi

  if ! command -v aws &> /dev/null; then
    echo "ERROR: AWS CLI is required but not installed"
    exit 1
  fi

  if ! pip show bedrock-agentcore-starter-toolkit &> /dev/null; then
    echo "Installing bedrock-agentcore-starter-toolkit..."
    pip install bedrock-agentcore-starter-toolkit
  fi

  echo "Prerequisites check complete"

  # Deploy MCP Server
  echo ""
  echo "Deploying MCP Server..."
  echo "=========================="

  cd src/mcp-servers/duckduckgo-mcp

  # Create ECR repository for MCP server if it doesn't exist
  MCP_REPO_NAME="lma-duckduckgo-mcp-server"
  aws ecr describe-repositories --repository-names $MCP_REPO_NAME --region $REGION 2>/dev/null || \
  aws ecr create-repository --repository-name $MCP_REPO_NAME --region $REGION

  # Build and push MCP server image
  echo "Building MCP server Docker image..."
  docker build -t $MCP_REPO_NAME .

  # Login to ECR
  aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR_REGISTRY

  # Tag and push image
  docker tag $MCP_REPO_NAME:latest $ECR_REGISTRY/$MCP_REPO_NAME:latest
  docker push $ECR_REGISTRY/$MCP_REPO_NAME:latest

  # Configure and deploy MCP server using AgentCore
  echo "Configuring MCP server for AgentCore..."

  # Create IAM role for MCP server if it doesn't exist
  MCP_ROLE_NAME="BedrockAgentCore-MCP-Server-Role"
  MCP_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${MCP_ROLE_NAME}"

  if ! aws iam get-role --role-name $MCP_ROLE_NAME 2>/dev/null; then
    echo "Creating IAM role for MCP server..."
    
    # Create trust policy
    cat > mcp-trust-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "bedrock-agentcore.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
EOF

    # Create execution policy
    cat > mcp-execution-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        }
    ]
}
EOF

    # Create role
    aws iam create-role \
        --role-name $MCP_ROLE_NAME \
        --assume-role-policy-document file://mcp-trust-policy.json

    # Attach execution policy
    aws iam put-role-policy \
        --role-name $MCP_ROLE_NAME \
        --policy-name "MCPServerExecutionPolicy" \
        --policy-document file://mcp-execution-policy.json

    # Clean up policy files
    rm mcp-trust-policy.json mcp-execution-policy.json
    
    echo "IAM role created: $MCP_ROLE_ARN"
  else
    echo "Using existing IAM role: $MCP_ROLE_ARN"
  fi

  # Configure MCP server
  agentcore configure \
      --entrypoint server.py \
      --execution-role $MCP_ROLE_ARN \
      --protocol MCP \
      --container-uri $ECR_REGISTRY/$MCP_REPO_NAME:latest

  # Deploy MCP server
  echo "Launching MCP server..."
  MCP_OUTPUT=$(agentcore launch)
  MCP_ARN=$(echo "$MCP_OUTPUT" | grep -o 'arn:aws:bedrock-agentcore:[^[:space:]]*' | head -1)

  echo "MCP Server deployed successfully!"
  echo "MCP Server ARN: $MCP_ARN"

  # Calculate MCP server URL
  MCP_ENCODED_ARN=$(echo $MCP_ARN | sed 's/:/%3A/g' | sed 's/\//%2F/g')
  MCP_SERVER_URL="https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${MCP_ENCODED_ARN}/invocations?qualifier=DEFAULT"

  echo "MCP Server URL: $MCP_SERVER_URL"

  cd ../../..

  # Deploy Strands Agent
  echo ""
  echo "Deploying Strands Agent..."
  echo "============================="

  cd src/strands-agent/search-agent

  # Create ECR repository for Strands agent if it doesn't exist
  AGENT_REPO_NAME="lma-strands-search-agent"
  aws ecr describe-repositories --repository-names $AGENT_REPO_NAME --region $REGION 2>/dev/null || \
  aws ecr create-repository --repository-name $AGENT_REPO_NAME --region $REGION

  # Build and push Strands agent image
  echo "Building Strands agent Docker image..."
  docker build -t $AGENT_REPO_NAME .

  # Tag and push image
  docker tag $AGENT_REPO_NAME:latest $ECR_REGISTRY/$AGENT_REPO_NAME:latest
  docker push $ECR_REGISTRY/$AGENT_REPO_NAME:latest

  # Create IAM role for Strands agent if it doesn't exist
  AGENT_ROLE_NAME="BedrockAgentCore-Strands-Agent-Role"
  AGENT_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${AGENT_ROLE_NAME}"

  if ! aws iam get-role --role-name $AGENT_ROLE_NAME 2>/dev/null; then
    echo "Creating IAM role for Strands agent..."
    
    # Create trust policy
    cat > agent-trust-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "bedrock-agentcore.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
EOF

    # Create execution policy
    cat > agent-execution-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "bedrock-agentcore:InvokeAgentRuntime"
            ],
            "Resource": "*"
        }
    ]
}
EOF

    # Create role
    aws iam create-role \
        --role-name $AGENT_ROLE_NAME \
        --assume-role-policy-document file://agent-trust-policy.json

    # Attach execution policy
    aws iam put-role-policy \
        --role-name $AGENT_ROLE_NAME \
        --policy-name "StrandsAgentExecutionPolicy" \
        --policy-document file://agent-execution-policy.json

    # Clean up policy files
    rm agent-trust-policy.json agent-execution-policy.json
    
    echo "IAM role created: $AGENT_ROLE_ARN"
  else
    echo "Using existing IAM role: $AGENT_ROLE_ARN"
  fi

  # Configure Strands agent with MCP server URL as environment variable
  echo "Configuring Strands agent for AgentCore..."

  agentcore configure \
      --entrypoint search_agent.py \
      --execution-role $AGENT_ROLE_ARN \
      --container-uri $ECR_REGISTRY/$AGENT_REPO_NAME:latest \
      --environment-variables MCP_SERVER_URL=$MCP_SERVER_URL

  # Deploy Strands agent
  echo "Launching Strands agent..."
  AGENT_OUTPUT=$(agentcore launch)
  AGENT_ARN=$(echo "$AGENT_OUTPUT" | grep -o 'arn:aws:bedrock-agentcore:[^[:space:]]*' | head -1)

  echo "Strands Agent deployed successfully!"
  echo "Strands Agent ARN: $AGENT_ARN"

  cd ../../..

  # Summary
  echo ""
  echo "Deployment Complete!"
  echo "======================="
  echo "MCP Server ARN: $MCP_ARN"
  echo "MCP Server URL: $MCP_SERVER_URL"
  echo "Strands Agent ARN: $AGENT_ARN"
  echo ""
  echo "Test your agent with:"
  echo "agentcore invoke '{\"prompt\": \"Search for the latest AWS services\"}' --agent-arn $AGENT_ARN"
  echo ""
  echo "Next steps:"
  echo "1. Test the deployed agent using the agentcore CLI"
  echo "2. Integrate with your application using the InvokeAgentRuntime API"
  echo "3. Monitor performance using CloudWatch logs"
  echo "4. Set up authentication if needed for production use"
fi

exit 0
