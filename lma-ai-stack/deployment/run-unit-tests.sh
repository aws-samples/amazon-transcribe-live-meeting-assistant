#!/bin/bash
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
# This assumes all of the OS-level configuration has been completed and git repo has already been cloned
#
# This script should be run from the repo's deployment directory
# cd deployment
# ./run-unit-tests.sh
#

# Get reference for all important folders
template_dir="$PWD"
source_dir="$template_dir/../source"

# Run unit tests
echo "Running unit tests"
echo "cd ../source"
cd ../source
echo "No unit tests to run, so sad ..."
echo "Completed unit tests"
