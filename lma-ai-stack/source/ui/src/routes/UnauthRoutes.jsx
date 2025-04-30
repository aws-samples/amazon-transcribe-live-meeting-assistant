// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import PropTypes from 'prop-types';
import { Redirect, Route, Switch } from 'react-router-dom';

import { AmplifyAuthContainer, AmplifyAuthenticator, AmplifySignIn, AmplifySignUp } from '@aws-amplify/ui-react';
import { Auth } from 'aws-amplify';
import { LOGIN_PATH, LOGOUT_PATH, REDIRECT_URL_PARAM } from './constants';

// this is set at build time depending on the AllowedSignUpEmailDomain CloudFormation parameter
const { REACT_APP_SHOULD_HIDE_SIGN_UP = 'true' } = process.env;

const UnauthRoutes = ({ location }) => {
  const handleMicrosoftSSO = async () => {
    try {
      // Trigger the hosted UI for Microsoft SSO
      await Auth.federatedSignIn({ provider: 'Microsoft' });
    } catch (error) {
      console.error('Error during Microsoft SSO:', error);
    }
  };
  return (
    <Switch>
      <Route path={LOGIN_PATH}>
        <AmplifyAuthContainer>
          <AmplifyAuthenticator>
            <AmplifySignIn
              headerText="Welcome to Live Meeting Assistant!"
              hideSignUp={REACT_APP_SHOULD_HIDE_SIGN_UP}
              slot="sign-in"
            />
            <button
              type="button"
              onClick={handleMicrosoftSSO}
              style={{
                marginTop: '20px',
                padding: '10px',
                backgroundColor: '#2F2F2F',
                color: '#FFFFFF',
                border: 'none',
                cursor: 'pointer',
              }}
            >
              Sign in with Microsoft
            </button>
            <AmplifySignUp
              headerText="Welcome to Live Meeting Assistant!"
              slot="sign-up"
              h
              usernameAlias="email"
              formFields={[
                {
                  type: 'email',
                  inputProps: { required: true, autocomplete: 'email' },
                },
                { type: 'password' },
              ]}
            />
          </AmplifyAuthenticator>
        </AmplifyAuthContainer>
      </Route>
      <Route path={LOGOUT_PATH}>
        <Redirect to={LOGIN_PATH} />
      </Route>
      <Route>
        <Redirect
          to={{
            pathname: LOGIN_PATH,
            search: `?${REDIRECT_URL_PARAM}=${location.pathname}${location.search}`,
          }}
        />
      </Route>
    </Switch>
  );
};
UnauthRoutes.propTypes = {
  location: PropTypes.shape({
    pathname: PropTypes.string,
    search: PropTypes.string,
  }).isRequired,
};

export default UnauthRoutes;
