#!/bin/bash
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
# This assumes all of the OS-level configuration has been completed and git repo has already been cloned
#
# This script should be run from the repo's deployment directory
# cd deployment
# ./build-s3-dist.sh source-bucket-base-name solution-name version-code
#
# Paramenters:
#  - source-bucket-base-name: Name for the S3 bucket location where the template will source the Lambda
#    code from. The template will append '-[region_name]' to this bucket name.
#    For example: ./build-s3-dist.sh solutions my-solution v1.0.0
#    The template will then expect the source code to be located in the solutions-[region_name] bucket
#
#  - solution-name: name of the solution for consistency
#
#  - version-code: version of the package

# Stop at the first failing step, and treat a failure anywhere in a pipeline as
# the pipeline's failure. Without this the script ran to the end regardless and
# exited on its closing `echo`, so a `sam build` that could not start still
# reported success to the caller -- and the stack's template was simply absent
# from S3, surfacing much later as a NoSuchKey when CloudFormation went looking
# for the nested template. A build that did not build has to say so.
set -eo pipefail

# Check to see if input has been provided:
if [ -z "$1" ]; then
    echo "usage: $0 <base source bucket name> [<solution name or s3 prefix>] [<version>] [<region>]"
    echo
    echo "Please provide the base source bucket name, trademark approved solution name and version where the lambda code will eventually reside."
    echo "For example: ./build-s3-dist.sh solutions trademarked-solution-name v1.0.0 us-east-1"
    exit 1
fi
OUT_DIR="../out"
export RELEASE_S3_BUCKET_BASE="$1"

export RELEASE_S3_PREFIX="${2:-artifacts/lma/lma-websocket}"

if [ ! -z "$3" ]; then
    export RELEASE_VERSION="${3}"
fi

if [ -z "$4" ]; then
    export AWS_REGION=${AWS_REGION:-us-east-1}
else
    export AWS_REGION="$4"
fi

[ -d ${OUT_DIR} ] && rm -fr ${OUT_DIR}
mkdir -p ${OUT_DIR}

# Cross-platform sha256sum
if ! command -v sha256sum >/dev/null 2>&1; then
  sha256sum() { shasum -a 256 "$@"; }
fi

cd ..

HASH=$(
  find . \( -name node_modules -o -name build \) -prune -o -type f -print0 | 
  sort -z |
  xargs -0 sha256sum |
  sha256sum |
  cut -d" " -f1 | 
  cut -c1-16
  )
ZIPFILE=lma-websocket-${HASH}.zip


zip -r out/${ZIPFILE} source/ -x source/app/node_modules/**\* source/app/dist/**\*
cd deployment

# git ls-files .. | xargs zip -@ --filesync ${OUT_DIR}/audiohooksrc.zip
export RELEASE_S3_BUCKET=${RELEASE_S3_BUCKET_BASE}-${AWS_REGION}
aws s3 cp ${OUT_DIR}/${ZIPFILE} s3://${RELEASE_S3_BUCKET}/${RELEASE_S3_PREFIX}/${RELEASE_VERSION}/${ZIPFILE}

TEMPLATE_FILE="./lma-websocket-transcriber.yaml"
RELEASE_S3_PREFIX_SUB=${RELEASE_S3_PREFIX////_}
PACKAGE_RELEASE_REPLACE_OUT_FILE=${OUT_DIR}/template-replaced-${RELEASE_S3_BUCKET}-${RELEASE_S3_PREFIX_SUB}-${RELEASE_VERSION}.yaml
sed -E \
		" \
		/^ {2,}BootstrapBucketBaseName:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 ${RELEASE_S3_BUCKET_BASE}@ ; \
		/^ {2,}BootstrapS3Prefix:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 ${RELEASE_S3_PREFIX}@ ; \
		/^ {2,}BootstrapVersion:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 ${RELEASE_VERSION}@ ; \
		" \
		${TEMPLATE_FILE} |
sed -e "s%<LMA_WEBSOCKET_ZIP_FILENAME>%$ZIPFILE%g"  > ${PACKAGE_RELEASE_REPLACE_OUT_FILE}

PACKAGE_RELEASE_FILE_NAME=template-packaged-${RELEASE_S3_BUCKET}-${RELEASE_S3_PREFIX_SUB}-${RELEASE_VERSION}.yaml
PACKAGE_RELEASE_OUT_FILE=${OUT_DIR}/${PACKAGE_RELEASE_FILE_NAME}

# Build the Lambda bundles inside SAM's build container by default, which pins
# the interpreter and produces manylinux wheels whatever the developer runs.
# SAM_BUILD_IN_CONTAINER=false builds with the local interpreter instead, for a
# builder that already IS the Lambda platform and has no Docker daemon to offer
# -- a linux/amd64 CI runner on python3.12. Same flag, same meaning, as
# lma-ai-stack/Makefile; see the longer note there.
SAM_BUILD_IN_CONTAINER="${SAM_BUILD_IN_CONTAINER:-true}"
if [ "${SAM_BUILD_IN_CONTAINER}" = "false" ]; then
	SAM_BUILD_CONTAINER_ARGS=()
else
	SAM_BUILD_CONTAINER_ARGS=(--use-container)
fi

echo "[INFO] sam building template file for release ${RELEASE_VERSION} (in container: ${SAM_BUILD_IN_CONTAINER})"
sam build \
	"${SAM_BUILD_CONTAINER_ARGS[@]}" \
	--parallel \
	--cached \
	--template-file ${PACKAGE_RELEASE_REPLACE_OUT_FILE}

echo "[INFO] sam packaging for release ${RELEASE_VERSION}"
sam package \
	--s3-bucket ${RELEASE_S3_BUCKET} \
	--s3-prefix ${RELEASE_S3_PREFIX}/${RELEASE_VERSION} \
	--output-template-file ${PACKAGE_RELEASE_OUT_FILE} 

RELEASE_TEMPLATE_S3_URL=s3://${RELEASE_S3_BUCKET}/${RELEASE_S3_PREFIX}/${RELEASE_VERSION}/template.yaml
RELEASE_UPLOAD_FILE=${OUT_DIR}/release-upload-${PACKAGE_RELEASE_FILE_NAME}.txt

echo "[INFO] uploading ${PACKAGE_RELEASE_OUT_FILE} to ${RELEASE_TEMPLATE_S3_URL}"
aws s3 cp ${PACKAGE_RELEASE_OUT_FILE} ${RELEASE_TEMPLATE_S3_URL} | tee ${RELEASE_UPLOAD_FILE}

echo "[INFO] CloudFormation template URL: https://${RELEASE_S3_BUCKET}.s3.amazonaws.com/${RELEASE_S3_PREFIX}/${RELEASE_VERSION}/template.yaml"