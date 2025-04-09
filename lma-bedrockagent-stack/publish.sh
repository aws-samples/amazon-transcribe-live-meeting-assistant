#!/bin/bash

# This script packages and uploads the Lambda functions for the Bedrock Agent stack

# Stop the publish process on failures
set -e

# use current directory name as template name
NAME=$(basename `pwd`)

USAGE="$0 <cfn_bucket> <cfn_prefix> <region> [public]"

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

# Export these variables for use in the Lambda packaging section
export ARTIFACT_BUCKET=$BUCKET
export ARTIFACT_PREFIX=$PREFIX

# Get the directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Create a temporary directory for packaging
TEMP_DIR=$(mktemp -d)

# Package the Lambda functions
echo "Packaging Lambda functions..."

# Package salesforce_function.py
echo "Packaging salesforce_function.py..."
cp "$SCRIPT_DIR/src/salesforce_function.py" "$TEMP_DIR/salesforce_function.py"
cd "$TEMP_DIR"
zip -r salesforce_function.py.zip salesforce_function.py
aws s3 cp salesforce_function.py.zip s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}/lma-bedrockagent-stack/src/salesforce_function.py.zip

# Package jira_function.py
echo "Packaging jira_function.py..."
cp "$SCRIPT_DIR/src/jira_function.py" "$TEMP_DIR/jira_function.py"
cd "$TEMP_DIR"
zip -r jira_function.py.zip jira_function.py
aws s3 cp jira_function.py.zip s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}/lma-bedrockagent-stack/src/jira_function.py.zip

# Package asana_function.py
echo "Packaging asana_function.py..."
cp "$SCRIPT_DIR/src/asana_function.py" "$TEMP_DIR/asana_function.py"
cd "$TEMP_DIR"
zip -r asana_function.py.zip asana_function.py
aws s3 cp asana_function.py.zip s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}/lma-bedrockagent-stack/src/asana_function.py.zip

# Clean up
rm -rf "$TEMP_DIR"

echo "Lambda functions packaged and uploaded successfully!"

# Create bucket if it doesn't already exist
if [ -z "$(aws s3api list-buckets --query 'Buckets[].Name' | grep "\"$BUCKET\"")" ]; then
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
exit 0
