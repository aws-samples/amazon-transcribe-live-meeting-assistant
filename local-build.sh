#!/bin/bash

# Local build script that mimics CodeBuild process
set -e

echo "Building lma-ai-stack locally (same as CodeBuild)..."

cd /home/ec2-user/LMA/lma-ai-stack

# Set environment variables (same as CodeBuild)
export RELEASE_S3_BUCKET_BASE=local-test-bucket
export AWS_REGION=us-east-1
export RELEASE_VERSION=$(cat VERSION 2>/dev/null || echo "local-build")

# Run the build part only (without S3 upload)
echo "Creating template replacement..."
mkdir -p out

sed -E \
    " \
    /^ {2,}BootstrapBucketBaseName:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 $RELEASE_S3_BUCKET_BASE@ ; \
    /^ {2,}BootstrapS3Prefix:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 release@ ; \
    /^ {2,}BootstrapVersion:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 $RELEASE_VERSION@ ; \
    s@src-hash.zip@src-local.zip@g ; \
    s@<BUILD_DATE_TIME>@$(date -u +"%Y-%m-%dT%H:%M:%SZ")@g ; \
    " \
    'deployment/lma-ai-stack.yaml' > 'out/template-replaced-local.yaml'

echo "Running SAM build (using same container as CodeBuild)..."
sam build \
    --use-container \
    --build-image public.ecr.aws/sam/build-python3.12:latest-x86_64 \
    --parallel \
    --cached \
    --template-file 'out/template-replaced-local.yaml'

echo ""
echo "✅ Build completed successfully!"
echo "📁 Built artifacts: .aws-sam/build/"
echo "📄 Built template: .aws-sam/build/template.yaml"
echo ""
echo "This matches the CodeBuild process (without S3 upload)"
