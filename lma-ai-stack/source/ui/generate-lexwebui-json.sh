#!/bin/bash
set -a
source .env
envsubst < public/lex-web-ui-loader-config-template.json > public/lex-web-ui-loader-config.json
set +a