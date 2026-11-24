/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useEffect } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { signOut } from 'aws-amplify/auth';

const logger = new ConsoleLogger('SignOutRedirect');

/** The logout route: signs out, then reloads into the sign-in page. */
const SignOutRedirect = () => {
  useEffect(() => {
    signOut()
      .catch((err) => logger.error('signOut error', err))
      .finally(() => {
        window.location.reload();
      });
  }, []);
  return null;
};

export default SignOutRedirect;
