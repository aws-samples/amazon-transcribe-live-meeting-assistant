import React from 'react';
import logo from './logo.svg';
import './Login.css';
import { Box, Button, Container, ContentLayout, Form, FormField, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';
import { useNavigation } from '../../context/NavigationContext';

function Login() {

  const { navigate } = useNavigation();

  const [login, setLogin] = React.useState("");
  const [password, setPassword] = React.useState("");

  return (
    <ContentLayout header={
      <div></div>
    }>
      <Container
        fitHeight={true}
        footer={
          <Box textAlign="center">
          <SpaceBetween direction="vertical" size={'l'}>
            <Button>Forgot password?</Button>
            <Button>Settings</Button>
          </SpaceBetween>
          </Box>
        }
      >
        <form onSubmit={e => e.preventDefault()}>
          <SpaceBetween size={'l'}>
            <div></div>
            <Grid gridDefinition={[{ colspan: 4, offset:4 }]}>
              <img className='logo' src='q_svg.svg'></img>
            </Grid>
            <Grid gridDefinition={[{ colspan: 10, offset: 1 }]}>
              <SpaceBetween size={'xs'}>
                <h2 className='header'>Amazon Live<br/>Meeting Assistant</h2>
                <p className='headerDesc'>with Amazon Q for Business</p>
              </SpaceBetween>
            </Grid>

            <Grid gridDefinition={[{ colspan: 10, offset: 1 }]}>
              <Input value={login} placeholder='Username' />
            </Grid>
            <Grid gridDefinition={[{ colspan: 10, offset: 1 }]}>
              <Input value={password} type='password'  placeholder='Password'/>
            </Grid>
            <Grid gridDefinition={[{ colspan: 6, offset: 3 }]}>
              <Button variant='primary' fullWidth={true} onClick={() => navigate('meeting')}>Login</Button>
            </Grid>
          </SpaceBetween>
        </form>
      </Container>
    </ContentLayout>
  );
}

export default Login;
