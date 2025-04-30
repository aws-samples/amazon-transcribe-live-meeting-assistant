/* eslint-disable */
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// The values in this file are generated in CodeBuild
// You can also create a .env.local file during development
// https://create-react-app.dev/docs/adding-custom-environment-variables/

const {
  REACT_APP_USER_POOL_ID,
  REACT_APP_USER_POOL_CLIENT_ID,
  REACT_APP_IDENTITY_POOL_ID,
  REACT_APP_APPSYNC_GRAPHQL_URL,
  REACT_APP_AWS_REGION,
  REACT_APP_CLOUDFRONT_DOMAIN,
  REACT_APP_COGNITO_DOMAIN
 } = process.env;

const awsmobile = {
    "aws_project_region": REACT_APP_AWS_REGION,
    "aws_cognito_identity_pool_id": REACT_APP_IDENTITY_POOL_ID,
    "aws_cognito_region": REACT_APP_AWS_REGION,
    "aws_user_pools_id": REACT_APP_USER_POOL_ID,
    "aws_user_pools_web_client_id": REACT_APP_USER_POOL_CLIENT_ID,

    "oauth": {
        "domain": `${REACT_APP_COGNITO_DOMAIN}.auth.${REACT_APP_AWS_REGION}.amazoncognito.com`,
        "scope": ["openid", "email", "profile"],
        "redirectSignIn": REACT_APP_CLOUDFRONT_DOMAIN, // e.g., "http://localhost:3000/"
        "redirectSignOut": REACT_APP_CLOUDFRONT_DOMAIN, // e.g., "http://localhost:3000/"
        "responseType": "code" // or "token" based on your setup
    },
    "aws_cognito_login_mechanisms": [
        "PREFERRED_USERNAME"
    ],
    "aws_cognito_signup_attributes": [
        "EMAIL"
    ],
    "aws_cognito_mfa_configuration": "OFF",
    "aws_cognito_mfa_types": [
        "SMS"
    ],
    "aws_cognito_password_protection_settings": {
        "passwordPolicyMinLength": 8,
        "passwordPolicyCharacters": []
    },
    "aws_cognito_verification_mechanisms": [
        "EMAIL"
    ],
    "aws_appsync_graphqlEndpoint": REACT_APP_APPSYNC_GRAPHQL_URL,
    "aws_appsync_region": REACT_APP_AWS_REGION,
    "aws_appsync_authenticationType": "AMAZON_COGNITO_USER_POOLS"
}

export default awsmobile;
