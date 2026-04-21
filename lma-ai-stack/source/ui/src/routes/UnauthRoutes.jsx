/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Authenticator } from '@aws-amplify/ui-react';

import { LOGIN_PATH, LOGOUT_PATH, REDIRECT_URL_PARAM } from './constants';
import OAuthCallback from '../components/mcp-servers/OAuthCallback';

// Set at build time via the AllowedSignUpEmailDomain CloudFormation parameter.
const VITE_SHOULD_HIDE_SIGN_UP = import.meta.env.VITE_SHOULD_HIDE_SIGN_UP ?? 'true';

const AuthHeader = () => <h1 style={{ textAlign: 'center', margin: '2rem 0' }}>Welcome to Live Meeting Assistant!</h1>;

const AuthPanel = () => (
  <Authenticator
    initialState="signIn"
    components={{ Header: AuthHeader }}
    services={{
      async validateCustomSignUp(formData) {
        if (formData.email) {
          return undefined;
        }
        return { email: 'Email is required' };
      },
    }}
    signUpAttributes={['email']}
    hideSignUp={VITE_SHOULD_HIDE_SIGN_UP === 'true'}
  />
);

const UnauthRoutes = ({ location }) => (
  <Routes>
    <Route path="/oauth/callback" element={<OAuthCallback />} />
    <Route path={LOGIN_PATH} element={<AuthPanel />} />
    <Route path={LOGOUT_PATH} element={<Navigate to={LOGIN_PATH} replace />} />
    <Route
      path="*"
      element={
        <Navigate
          to={{
            pathname: LOGIN_PATH,
            search: `?${REDIRECT_URL_PARAM}=${location.pathname}${location.search}`,
          }}
          replace
        />
      }
    />
  </Routes>
);

UnauthRoutes.propTypes = {
  location: PropTypes.shape({
    pathname: PropTypes.string,
    search: PropTypes.string,
  }).isRequired,
};

export default UnauthRoutes;
