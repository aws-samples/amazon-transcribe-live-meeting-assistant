/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useState, useEffect } from 'react';
import Amplify from 'aws-amplify';
import awsExports from '../aws-exports';

const useAwsConfig = () => {
  const [awsConfig, setAwsConfig] = useState();
  useEffect(() => {
    Amplify.configure(awsExports);
    setAwsConfig(awsExports);
  }, [awsExports]);
  return awsConfig;
};

export default useAwsConfig;
