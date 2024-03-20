import React from 'react';
import ReactDOM from 'react-dom/client';
import "@cloudscape-design/global-styles/index.css"
import './index.css';
import App from './App';
import reportWebVitals from './reportWebVitals';
import { Theme, applyTheme } from '@cloudscape-design/components/theming';
import NavigationProvider from './context/NavigationContext';
import SettingsProvider from './context/SettingsContext';
import UserProvider from './context/UserContext';
import IntegrationProvider from './context/ProviderIntegrationContext';

const theme: Theme = {
  tokens: {
    colorTextButtonPrimaryDefault: {
      light: 'grey-900',
      dark: 'grey-900'
    },
    colorBackgroundButtonPrimaryDefault: {
      light: "#FF9900",
      dark: '#FF9900'
    },
    colorBackgroundButtonPrimaryActive: {
      light: "#FF9900",
      dark: '#FF9900'
    },
    colorBackgroundButtonPrimaryHover: {
      light: "#FF9900",
      dark: '#FF9900'
    },
    colorTextButtonPrimaryActive: {
      light: "#grey-900",
      dark: '#grey-900'
    },
    colorTextButtonPrimaryHover: {
      light: "#grey-900",
      dark: '#grey-900'
    }
  },
  contexts: {
    header: {
      tokens: {
        colorTextButtonPrimaryDefault: {
          light: 'grey-900',
          dark: 'grey-900'
        },
        colorBackgroundButtonPrimaryDefault: {
          light: "#FF9900",
          dark: '#FF9900'
        },
        colorBackgroundButtonPrimaryActive: {
          light: "#FF9900",
          dark: '#FF9900'
        }
      }
    }
  }
};
const { reset } = applyTheme({ theme });

const root = ReactDOM.createRoot(
  document.getElementById('root') as HTMLElement
);
root.render(
  <React.StrictMode>
    <SettingsProvider>
      <UserProvider>
        <NavigationProvider>
          <IntegrationProvider>
            <App />
          </IntegrationProvider>
        </NavigationProvider>
      </UserProvider>
    </SettingsProvider>
  </React.StrictMode>
);

// If you want to start measuring performance in your app, pass a function
// to log results (for example: reportWebVitals(console.log))
// or send to an analytics endpoint. Learn more: https://bit.ly/CRA-vitals
reportWebVitals();
