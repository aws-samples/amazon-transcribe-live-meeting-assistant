#!/bin/bash

# Test local build without S3 upload
cd /home/ec2-user/LMA/lma-ai-stack

echo "Testing local build process..."

# Set required environment variables
export RELEASE_S3_BUCKET_BASE=test-bucket
export AWS_REGION=us-east-1
export RELEASE_VERSION=test-version

# Create the template replacement (this is what the Makefile does)
mkdir -p out

echo "Creating template replacement..."
sed -E \
    " \
    /^ {2,}BootstrapBucketBaseName:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 $RELEASE_S3_BUCKET_BASE@ ; \
    /^ {2,}BootstrapS3Prefix:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 release@ ; \
    /^ {2,}BootstrapVersion:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 $RELEASE_VERSION@ ; \
    s@src-hash.zip@src-test.zip@g ; \
    s@<BUILD_DATE_TIME>@$(date -u +"%Y-%m-%dT%H:%M:%SZ")@g ; \
    " \
    'deployment/lma-ai-stack.yaml' > 'out/template-replaced-test.yaml'

echo "Running SAM build..."
sam build \
    --use-container \
    --build-image public.ecr.aws/sam/build-python3.12:latest-x86_64 \
    --parallel \
    --cached \
    --template-file 'out/template-replaced-test.yaml'

echo "Build completed successfully!"
echo "Built artifacts are in: .aws-sam/build/"
echo "Built template is: .aws-sam/build/template.yaml"
