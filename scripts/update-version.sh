#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Updates versions in source files. Used to bump semantic versions in a release

set -eu -o pipefail

export NEW_VERSION=${1:-}

[[ -z "$NEW_VERSION" ]] && {
    echo "usage: $0 '<new version>'" >&2
    exit 1
}

# files to be modified are declared relative to the script path
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
ROOT_DIR=$(readlink -f "${SCRIPT_DIR}/..")
AI_STACK_DIR="${ROOT_DIR}/lca-ai-stack"

# files to be modified
# values can be overridden by existing environment vars values
VERSION_FILE=${VERSION_FILE:-"${ROOT_DIR}/VERSION"}
AI_STACK_VERSION_FILE=${AI_STACK_VERSION_FILE:-"${AI_STACK_DIR}/VERSION"}
TEMPLATE_FILE=${TEMPLATE_FILE:-"${ROOT_DIR}/lca-main.yaml"}
AI_STACK_TEMPLATE_FILE=${AI_STACK_TEMPLATE_FILE:-"${AI_STACK_DIR}/deployment/lca-ai-stack.yaml"}
SAMCONFIG_FILE=${SAMCONFIG_FILE:-"${AI_STACK_DIR}/samconfig.toml"}
UI_PACKAGE_JSON_FILE=${UI_PACKAGE_JSON_FILE:-"${AI_STACK_DIR}/source/ui/package.json"}
UI_PACKAGE_LOCK_JSON_FILE=${UI_PACKAGE_LOCK_JSON_FILE:-"${AI_STACK_DIR}/source/ui/package-lock.json"}

export VERSION_REGEX="${VERSION_REGEX:-$'((0|[1-9]\d*)\.){2\}(0|[1-9]\d*)'}"

if [[ -f "$VERSION_FILE" ]] ; then
    sed --in-place --regexp-extended --expression "$(
        # shellcheck disable=SC2016
        echo 's/^${VERSION_REGEX}$/${NEW_VERSION}/' | \
        envsubst '${VERSION_REGEX} ${NEW_VERSION}' \
    )" "$VERSION_FILE"
else
    echo "[WARNING] ${VERSION_FILE} file does not exist" >&2
fi

if [[ -f "$AI_STACK_VERSION_FILE" ]] ; then
    sed --in-place --regexp-extended --expression "$(
        # shellcheck disable=SC2016
        echo 's/^${VERSION_REGEX}$/${NEW_VERSION}/' | \
        envsubst '${VERSION_REGEX} ${NEW_VERSION}' \
    )" "$AI_STACK_VERSION_FILE"
else
    echo "[WARNING] ${AI_STACK_VERSION_FILE} file does not exist" >&2
fi

if [[ -f "$TEMPLATE_FILE" ]] ; then
    sed --in-place --regexp-extended --expression "$(
        # shellcheck disable=SC2016
        echo '
          s/^(Description: .+\(v)${VERSION_REGEX}(\).*)$/\1${NEW_VERSION}\5/;
        ' | \
        envsubst '${VERSION_REGEX} ${NEW_VERSION}' \
    )" "$TEMPLATE_FILE"
else
    echo "[WARNING] ${TEMPLATE_FILE} file does not exist" >&2
fi

if [[ -f "$AI_STACK_TEMPLATE_FILE" ]] ; then
    sed --in-place --regexp-extended --expression "$(
        # shellcheck disable=SC2016
        echo '
		  /^ {2,}BootstrapVersion:/ , /^ {2,}Default:/ {
            s/^(.*Default: {1,})${VERSION_REGEX}(.*)/\1${NEW_VERSION}\5/;
          }
        ' | \
        envsubst '${VERSION_REGEX} ${NEW_VERSION}' \
    )" "$AI_STACK_TEMPLATE_FILE"
else
    echo "[WARNING] ${AI_STACK_TEMPLATE_FILE} file does not exist" >&2
fi

if [[ -f "$SAMCONFIG_FILE" ]] ; then
    sed --in-place --regexp-extended --expression "$(
        # shellcheck disable=SC2016
        echo '
          s/^( *s3_prefix *=.*)${VERSION_REGEX}(.*)$/\1${NEW_VERSION}\5/;
          s/^( *"BootstrapVersion=)${VERSION_REGEX}(.*)$/\1${NEW_VERSION}\5/;
        ' | \
        envsubst '${VERSION_REGEX} ${NEW_VERSION}' \
    )" "$SAMCONFIG_FILE"
else
    echo "[WARNING] ${SAMCONFIG_FILE} file does not exist" >&2
fi

if [[ -f "$UI_PACKAGE_JSON_FILE" ]] ; then
    sed --in-place --regexp-extended --expression "$(
        # shellcheck disable=SC2016
        echo '
		  /^ *"name" *: *"lca-ui" *, *$/ , /^ *"version" *:/ {
            s/^(.*"version" *: *")${VERSION_REGEX}(.*)/\1${NEW_VERSION}\5/;
          }
        ' | \
        envsubst '${VERSION_REGEX} ${NEW_VERSION}' \
    )" "$UI_PACKAGE_JSON_FILE"
else
    echo "[WARNING] ${UI_PACKAGE_JSON_FILE} file does not exist" >&2
fi

if [[ -f "$UI_PACKAGE_LOCK_JSON_FILE" ]] ; then
    sed --in-place --regexp-extended --expression "$(
        # shellcheck disable=SC2016
        echo '
		  /^ *"name" *: *"lca-ui" *, *$/ , /^ *"version" *:/ {
            s/^(.*"version" *: *")${VERSION_REGEX}(.*)/\1${NEW_VERSION}\5/;
          }
        ' | \
        envsubst '${VERSION_REGEX} ${NEW_VERSION}' \
    )" "$UI_PACKAGE_LOCK_JSON_FILE"
else
    echo "[WARNING] ${UI_PACKAGE_LOCK_JSON_FILE} file does not exist" >&2
fi
