import { useEffect, useState } from 'react';
import logo from './logo.svg';
import './LoginCognito.css';
import { Box, Button, Container, ContentLayout, Form, FormField, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';
import { useNavigation } from '../../context/NavigationContext';
import { useUserContext } from '../../context/UserContext';

function LoginCognito() {

  const { navigate } = useNavigation();
  const { login,loggedIn, exchangeCodeForToken, checkTokenExpired } = useUserContext();

  const queryParameters = new URLSearchParams(window.location.search);
  const code = queryParameters.get("code");

  const [ version, setVersion ] = useState("");
  useEffect(() => {
    if (chrome && chrome.runtime) {
      const manifestData = chrome.runtime.getManifest();
      setVersion(manifestData.version)
    } else {
      setVersion("dev/web");
    }
  }, [version, setVersion]);

  if (code && !loggedIn) {
    exchangeCodeForToken(code, 'authorization_code');
  }

  return (
    <ContentLayout header={
      <div></div>
    }>
      <Container
        fitHeight={true}
        footer={''}
      >
        <SpaceBetween size={'l'}>
          <div></div>
          <Grid gridDefinition={[{ colspan: 4, offset: 4 }]}>
            <img className='logo' src='q_svg.svg'></img>
          </Grid>
          <Grid gridDefinition={[{ colspan: 10, offset: 1 }]}>
            <SpaceBetween size={'xs'}>
              <h2 className='header'>Amazon Live<br />Meeting Assistant</h2>
              <p className='headerDesc'>Powered by Amazon Transcribe and Amazon Bedrock</p>
            </SpaceBetween>
          </Grid>
          <Grid gridDefinition={[{ colspan: 6, offset: 3 }]}>
            <Button variant='primary' fullWidth={true} onClick={() => login()}>Login</Button>
          </Grid>
          <Grid gridDefinition={[{ colspan: 10, offset: 1 }]}>
            <div className='version'>{version}</div>
          </Grid>
        </SpaceBetween>
      </Container>
    </ContentLayout>
  );
}

export default LoginCognito;
