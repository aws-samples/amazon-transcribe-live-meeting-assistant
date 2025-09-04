#!/bin/bash

# Test Deployment Build Script
# This tests the build process without changing Node.js version

echo "=== Testing LMA UI Build (Deployment Style) ==="
echo "Current Node.js version: $(node --version)"
echo "Current NPM version: $(npm --version)"
echo

# Set up environment variables (same as CodeBuild)
export IMAGE_TAG="local-build"
export CODEBUILD_SRC_DIR=$(pwd)

echo "Current directory: $(pwd)"

# Navigate to UI directory
echo "Installing Web UI dependencies..."
cd lma-ai-stack/source/ui

# Install dependencies (same as CodeBuild)
echo "Running npm install..."
npm install

echo "Performing token replacement for React build..."
cd $CODEBUILD_SRC_DIR

# Token replacement (same as CodeBuild)
echo "Replacing VERSION_TOKEN with actual version in source files"
sed -i.bak "s/<VERSION_TOKEN>/${IMAGE_TAG}/g" lma-ai-stack/source/ui/src/components/common/constants.js 2>/dev/null || echo "constants.js not found, skipping"
sed -i.bak "s/<VERSION_TOKEN>/${IMAGE_TAG}/g" lma-ai-stack/source/ui/package.json
sed -i.bak "s/<VERSION_TOKEN>/${IMAGE_TAG}/g" lma-ai-stack/source/ui/package-lock.json 2>/dev/null || true
sed -i.bak "s/<VERSION_TOKEN>/${IMAGE_TAG}/g" VERSION 2>/dev/null || echo "VERSION file not found, skipping"

echo "Verification - checking replaced values:"
grep "LMA_VERSION" lma-ai-stack/source/ui/src/components/common/constants.js 2>/dev/null || echo "LMA_VERSION not found in constants.js"
grep "version" lma-ai-stack/source/ui/package.json | head -1 || echo "version not found in package.json"

# Build phase (same as CodeBuild)
echo "Build started on $(date)"
cd $CODEBUILD_SRC_DIR
cd lma-ai-stack/source/ui

echo "Building Web UI with deployment settings..."
npm run build

BUILD_EXIT_CODE=$?

echo "Build completed on $(date)"
echo "Build exit code: $BUILD_EXIT_CODE"

if [ $BUILD_EXIT_CODE -eq 0 ]; then
    echo "✅ BUILD SUCCESSFUL"
    echo "Listing build output:"
    find build -name "*.js" | head -10
    echo "Build directory size: $(du -sh build)"
else
    echo "❌ BUILD FAILED"
    echo "This matches the deployment error you're seeing!"
fi

echo
echo "=== Build Test Complete ==="
echo "Exit code: $BUILD_EXIT_CODE"
