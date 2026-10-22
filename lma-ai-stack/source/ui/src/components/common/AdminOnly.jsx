/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Alert, Box, Spinner } from '@cloudscape-design/components';

import useUserGroups from '../../hooks/use-user-groups';

/**
 * What a non-admin sees in place of an administrative form.
 *
 * `feature` names what the page configures, so the message says which setting
 * to ask an administrator about rather than just "access denied".
 *
 * The message is about *changing* the setting, which is what the group
 * restriction covers. Several of these values are read by every user through
 * some other surface -- the Transcription Engine selection decides the capture
 * sample rate on the Stream Audio form, and the chat buttons are rendered in
 * every viewer's chat panel -- so saying only an Admin may see them would be
 * wrong. Nor does this say the setting applies to every meeting in the account:
 * it is true of the configuration records but not of user management, which
 * uses this same component.
 */
export const AccessDenied = ({ feature = undefined }) => (
  <Box padding="xl">
    <Alert type="error" header="Access denied">
      Only <strong>Admin</strong> users can change{feature ? ` ${feature}` : ' this configuration'}. Ask an
      administrator if you need a change made.
    </Alert>
  </Box>
);

AccessDenied.propTypes = { feature: PropTypes.string };

/**
 * Renders `children` only for a member of the Admin Cognito group.
 *
 * Group membership is read from the session, which resolves asynchronously, so
 * until it has there is no answer to act on: rendering the denial during that
 * window would flash an error at an admin on every load, and rendering the
 * children would flash the form at everybody else. Both wait for the session,
 * which is why this is a wrapper rather than an `isAdmin &&` branch.
 *
 * `children` are not mounted for a non-admin, so an administrative page's data
 * fetching never starts for one.
 *
 * This is presentation. The constraint itself lives on the AppSync field and in
 * the resolver behind it; this is what makes it legible instead of arriving as a
 * failed save.
 */
const AdminOnly = ({ children, feature = undefined }) => {
  const { isAdmin, isLoading } = useUserGroups();

  if (isLoading) {
    return (
      <Box padding="xl" textAlign="center">
        <Spinner size="large" /> Loading...
      </Box>
    );
  }

  if (!isAdmin) {
    return <AccessDenied feature={feature} />;
  }

  return children;
};

AdminOnly.propTypes = {
  children: PropTypes.node.isRequired,
  feature: PropTypes.string,
};

export default AdminOnly;
