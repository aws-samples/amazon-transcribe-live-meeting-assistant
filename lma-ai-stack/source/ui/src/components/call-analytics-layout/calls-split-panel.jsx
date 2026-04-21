/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { useLocation } from 'react-router-dom';
import { CALLS_PATH } from '../../routes/constants';

import CallListSplitPanel from '../call-list/CallListSplitPanel';

const CallsSplitPanel = () => {
  const { pathname } = useLocation();
  if (pathname !== CALLS_PATH && pathname !== `${CALLS_PATH}/`) {
    return null;
  }
  return <CallListSplitPanel />;
};

export default CallsSplitPanel;
