import React from 'react';
import logo from './logo.svg';
import './AssistantMessage.css'
import { Box, Button, Container, ContentLayout, ExpandableSection, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';
import { Theme, applyTheme } from '@cloudscape-design/components/theming';
import { applyMode, Mode } from '@cloudscape-design/global-styles';

function AssistantMessage({ responseTo, message }: any) {
  return (  
    <Grid disableGutters gridDefinition={[{ colspan: 1 }, { colspan: 10 }, { colspan: 1 }]}>
      <img className='assistantLogo' src='q_svg.svg'></img>
      <div className='assistantBox'>
        <Box>{message}</Box>
      </div>
      <div></div>
    </Grid>
  );
}

export default AssistantMessage;
