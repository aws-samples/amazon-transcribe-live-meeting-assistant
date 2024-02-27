import React from 'react';
import logo from './logo.svg';
import './UserMessage.css'
import { Box, Button, Container, ContentLayout, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';

function UserMessage() {

  const [input, setInput] = React.useState("");

  return (  
    <SpaceBetween size={'xxxs'}>
      <Grid gridDefinition={[{ colspan: 10, offset: 2 }]}>
        <Box variant='p' float='right'>Chris L (you)</Box>
      </Grid>
      <Grid gridDefinition={[{ colspan: 10, offset: 2 }]}>
        <div className='userBox'>
          Hey guys, this is a meeting comment from the user
        </div>
      </Grid>
    </SpaceBetween>
  );
}

export default UserMessage;
