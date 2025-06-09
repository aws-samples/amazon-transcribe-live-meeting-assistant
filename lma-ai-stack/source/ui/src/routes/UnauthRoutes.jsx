// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect } from 'react';
import PropTypes from 'prop-types';
import { Redirect, Route, Switch } from 'react-router-dom';
import { AmplifyAuthContainer, AmplifyAuthenticator, AmplifySignIn } from '@aws-amplify/ui-react';

// import { AmplifyAuthContainer, AmplifyAuthenticator, AmplifySignIn, AmplifySignUp } from '@aws-amplify/ui-react';
// import { Auth } from 'aws-amplify';
import { LOGIN_PATH, LOGOUT_PATH, REDIRECT_URL_PARAM } from './constants';

// this is set at build time depending on the AllowedSignUpEmailDomain CloudFormation parameter
// const { REACT_APP_SHOULD_HIDE_SIGN_UP = 'true' } = process.env;

const UnauthRoutes = ({ location }) => {
  // const handleMicrosoftSSO = async () => {
  //   try {
  //     // Trigger the hosted UI for Microsoft SSO
  //     await Auth.federatedSignIn({ provider: 'Microsoft' });
  //   } catch (error) {
  //     console.error('Error during Microsoft SSO:', error);
  //   }
  // };
  useEffect(() => {
    // Function to inject styles into shadow root
    const injectStyles = () => {
      const styleSheet = document.createElement('style');
      styleSheet.textContent = `
        amplify-amazon-button {
          display: none !important;
        }
        amplify-federated-buttons {
          display: none !important;
        }
        amplify-federated-sign-in {
          display: none !important;
        }
        .amplify-button[data-variation='primary'][data-provider='amazon'] {
          display: none !important;
        }
        .amplify-button[data-variation='primary'][data-provider='Amazon'] {
          display: none !important;
        }
      `;

      // Find all shadow roots in the document
      const shadowRoots = Array.from(document.querySelectorAll('*'))
        .map((el) => el.shadowRoot)
        .filter(Boolean);

      // Inject styles into each shadow root
      shadowRoots.forEach((root) => {
        if (!root.querySelector('style[data-amplify-styles]')) {
          const shadowStyle = styleSheet.cloneNode(true);
          shadowStyle.setAttribute('data-amplify-styles', '');
          root.appendChild(shadowStyle);
        }
      });
    };

    // Initial injection
    injectStyles();

    // Set up a mutation observer to watch for new shadow roots
    const observer = new MutationObserver((mutations) => {
      mutations.forEach((mutation) => {
        if (mutation.addedNodes.length) {
          injectStyles();
        }
      });
    });

    observer.observe(document.body, {
      childList: true,
      subtree: true,
    });

    return () => observer.disconnect();
  }, []);

  return (
    <Switch>
      <Route path={LOGIN_PATH}>
        <AmplifyAuthContainer>
          <AmplifyAuthenticator
            hideSignUp
            hideFederatedSignIn
            federatedSignInConfig={{
              amazon: {
                display: false,
              },
            }}
          >
            <AmplifySignIn
              headerText="Welcome to Live Meeting Assistant!"
              hideSignUp
              hideFederatedSignIn
              federatedSignInConfig={{
                amazon: {
                  display: false,
                },
              }}
              slot="sign-in"
            />
            {/* 
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
              usernameAlias="email"
              formFields={[
                {
                  type: 'email',
                  inputProps: { required: true, autocomplete: 'email' },
                },
                { type: 'password' },
              ]}
            />
            */}
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
