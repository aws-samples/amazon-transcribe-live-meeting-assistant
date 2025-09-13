#!/bin/bash
# Build script for Strands Lambda Layer
# This script creates a Lambda layer with the Strands SDK

set -e

echo "Building Strands Lambda Layer..."

# Create temporary directory
TEMP_DIR=$(mktemp -d)
echo "Using temporary directory: $TEMP_DIR"

# Copy requirements.txt to temp directory
cp strands_layer/requirements.txt $TEMP_DIR/

# Create python directory structure
mkdir -p $TEMP_DIR/python

# Install dependencies
echo "Installing Strands SDK and dependencies..."
pip3 install -r $TEMP_DIR/requirements.txt -t $TEMP_DIR/python/

# Create the layer zip file
echo "Creating layer zip file..."
cd $TEMP_DIR
zip -r strands-layer.zip python/

# Move zip file to strands_layer directory
mv strands-layer.zip ../strands_layer/

echo "Strands layer built successfully: strands_layer/strands-layer.zip"

# Cleanup
rm -rf $TEMP_DIR

echo "Build complete!"
