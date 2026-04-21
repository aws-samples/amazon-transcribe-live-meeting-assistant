/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * NOTE: This hook previously wrapped the legacy Amplify v1 `onAuthUIStateChange`
 * callback. With Amplify v6 we use `useAuthenticator` from @aws-amplify/ui-react.
 * This shim is kept so existing callers (if any remain) continue to work.
 */
import { useAuthenticator } from '@aws-amplify/ui-react';

const useUserAuthState = () => {
  const { authStatus, user } = useAuthenticator((context) => [context.authStatus, context.user]);
  return { authState: authStatus, user };
};

export default useUserAuthState;
