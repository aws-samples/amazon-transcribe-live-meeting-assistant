/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { MY_CONSENTS_PATH } from '../../routes/constants';

/** My consents' breadcrumbs, from the role's home page. */
const Breadcrumbs = ({ homeHref = '#/' }) => (
  <BreadcrumbGroup
    items={[
      { text: 'Meeting Assistant', href: homeHref },
      { text: 'My consents', href: `#${MY_CONSENTS_PATH}` },
    ]}
    ariaLabel="Breadcrumbs"
  />
);

Breadcrumbs.propTypes = { homeHref: PropTypes.string };

export default Breadcrumbs;
