#!/bin/bash

# Simple local build script that works exactly like CodeBuild
set -e

echo "🔨 Building lma-ai-stack locally..."
echo "This uses the same process as CodeBuild"
echo ""

# Use the deployment template directly (like CodeBuild does)
TEMPLATE_FILE="deployment/lma-ai-stack.yaml"

if [ ! -f "$TEMPLATE_FILE" ]; then
    echo "❌ Template file not found: $TEMPLATE_FILE"
    exit 1
fi

echo "📋 Template: $TEMPLATE_FILE"
echo "🐳 Container: public.ecr.aws/sam/build-python3.12:latest-x86_64"
echo ""

# Build using SAM (same as CodeBuild)
sam build \
    --use-container \
    --build-image public.ecr.aws/sam/build-python3.12:latest-x86_64 \
    --parallel \
    --cached \
    --template-file "$TEMPLATE_FILE"

echo ""
echo "✅ Build completed successfully!"
echo "📁 Built artifacts: .aws-sam/build/"
echo "📄 Built template: .aws-sam/build/template.yaml"
echo ""
echo "🎯 This matches exactly what CodeBuild does for the build phase"
