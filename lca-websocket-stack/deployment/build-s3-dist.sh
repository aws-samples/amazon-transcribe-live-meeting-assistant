# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#!/bin/bash
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

export RELEASE_S3_PREFIX="${2:-artifacts/lca/lca-websocket}"

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

cd ..
zip -r out/lca-websocket.zip source/ -x source/app/node_modules/**\* source/app/dist/**\*
cd deployment

# git ls-files .. | xargs zip -@ --filesync ${OUT_DIR}/audiohooksrc.zip
export RELEASE_S3_BUCKET=${RELEASE_S3_BUCKET_BASE}-${AWS_REGION}
aws s3 cp ${OUT_DIR}/lca-websocket.zip s3://${RELEASE_S3_BUCKET}/${RELEASE_S3_PREFIX}/${RELEASE_VERSION}/lca-websocket.zip

TEMPLATE_FILE="./lca-websocket.yaml"
RELEASE_S3_PREFIX_SUB=${RELEASE_S3_PREFIX////_}
PACKAGE_RELEASE_REPLACE_OUT_FILE=${OUT_DIR}/template-replaced-${RELEASE_S3_BUCKET}-${RELEASE_S3_PREFIX_SUB}-${RELEASE_VERSION}.yaml
sed -E \
		" \
		/^ {2,}BootstrapBucketBaseName:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 ${RELEASE_S3_BUCKET_BASE}@ ; \
		/^ {2,}BootstrapS3Prefix:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 ${RELEASE_S3_PREFIX}@ ; \
		/^ {2,}BootstrapVersion:/ , /^ {2,}Default:/ s@^(.*Default: {1,})(.*)@\1 ${RELEASE_VERSION}@ ; \
		" \
		${TEMPLATE_FILE} > ${PACKAGE_RELEASE_REPLACE_OUT_FILE}

PACKAGE_RELEASE_FILE_NAME=template-packaged-${RELEASE_S3_BUCKET}-${RELEASE_S3_PREFIX_SUB}-${RELEASE_VERSION}.yaml
PACKAGE_RELEASE_OUT_FILE=${OUT_DIR}/${PACKAGE_RELEASE_FILE_NAME}

echo "[INFO] sam building template file for release ${RELEASE_VERSION}"
sam build \
	--use-container \
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