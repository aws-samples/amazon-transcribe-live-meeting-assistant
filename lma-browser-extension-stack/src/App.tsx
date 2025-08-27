/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
 * or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
 * 
 */
import React, { useState } from 'react';
import logo from './logo.svg';
import './App.css';

import { Alert, AppLayout, Button, Container, ContentLayout, Header, Link, SpaceBetween } from '@cloudscape-design/components';
import Meeting from './components/screens/Meeting';
import Login from './components/screens/Login';
import { useNavigation } from './context/NavigationContext';
import Capture from './components/screens/Capture';
import LoginCognito from './components/screens/LoginCognito';
import { useUserContext } from './context/UserContext';

function App() {

  const { currentScreen } = useNavigation();
  const { user, loggedIn } = useUserContext();

  return (
    <AppLayout
      className='lmaAppLayout'
      navigationHide={true}
      toolsHide={true}
      content={
        (loggedIn ? <Capture /> : <LoginCognito />)
      }
    />
  );
}

export default App;
