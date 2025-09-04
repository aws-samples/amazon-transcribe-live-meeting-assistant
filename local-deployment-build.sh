#!/bin/bash

# Local Deployment Build Script
# This replicates the exact build process used in AWS CodeBuild during deployment

echo "=== LMA UI Local Deployment Build ==="
echo "This script replicates the exact build process used in AWS CodeBuild"
echo

# Set up environment (similar to CodeBuild)
export IMAGE_TAG="local-build"
export CODEBUILD_SRC_DIR=$(pwd)

echo "Current directory: $(pwd)"
echo "Installing NodeJS 18.19.1..."

# Install Node.js 18.19.1 (same version as CodeBuild)
if ! command -v n &> /dev/null; then
    echo "Installing 'n' (Node.js version manager)..."
    npm install -g n
fi

n 18.19.1
npm install -g npm@10.2.4

echo "Node.js version: $(node --version)"
echo "NPM version: $(npm --version)"

# Navigate to UI directory
echo "Installing Web UI dependencies..."
cd lma-ai-stack/source/ui

# Install dependencies (same as CodeBuild)
npm install

echo "Performing token replacement for React build..."
cd $CODEBUILD_SRC_DIR

# Token replacement (same as CodeBuild)
echo "Replacing VERSION_TOKEN with actual version in source files"
sed -i "s/<VERSION_TOKEN>/${IMAGE_TAG}/g" lma-ai-stack/source/ui/src/components/common/constants.js 2>/dev/null || echo "constants.js not found, skipping"
sed -i "s/<VERSION_TOKEN>/${IMAGE_TAG}/g" lma-ai-stack/source/ui/package.json
sed -i "s/<VERSION_TOKEN>/${IMAGE_TAG}/g" lma-ai-stack/source/ui/package-lock.json 2>/dev/null || true
sed -i "s/<VERSION_TOKEN>/${IMAGE_TAG}/g" VERSION 2>/dev/null || echo "VERSION file not found, skipping"

echo "Verification - checking replaced values:"
grep "LMA_VERSION" lma-ai-stack/source/ui/src/components/common/constants.js 2>/dev/null || echo "LMA_VERSION not found in constants.js"
grep "version" lma-ai-stack/source/ui/package.json | head -1 || echo "version not found in package.json"

# Build phase (same as CodeBuild)
echo "Build started on $(date)"
cd $CODEBUILD_SRC_DIR
cd lma-ai-stack/source/ui

echo "Building Web UI..."
npm run build

echo "Build completed on $(date)"
echo "Listing build output:"
find build -ls | head -20

echo
echo "=== Build Complete ==="
echo "If you see build files listed above, the build was successful!"
echo "This is the exact same process used in AWS CodeBuild deployment."
