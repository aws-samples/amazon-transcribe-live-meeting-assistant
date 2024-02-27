import React, { createContext, useContext, useEffect, useState } from 'react';
import { useSettings } from './SettingsContext';
import { isToken } from 'typescript';

type User = {
  id_token?: string,
  access_token?: string,
  refresh_token?: string
};

const initialUserContext = {
  user: {} as User,
  login: () => {},
  logout: () => { },
  exchangeCodeForToken: (code:string) => {},
  loggedIn: false
};
const UserContext = createContext(initialUserContext);

function UserProvider({ children }: any) {
  const [user, setUser] = useState<User>({});
  const [userName, setUserName] = useState("");
  const settings = useSettings();
  const [loggedIn, setLoggedIn] = useState(false);

  const isTokenExpired = (jwtToken:string) => {
    const [, payload] = jwtToken.split('.');
    const { exp: expires } = JSON.parse(atob(payload));
    if (typeof expires === 'number') {
      let expiryDate = new Date(expires * 1000);
      console.log("expiry:", expiryDate);
      return (expiryDate < new Date());
    }
    return true;
  }

  // Load user
  useEffect(() => {
    if (chrome.storage) {
      chrome.storage.local.get('authTokens', (result) => {
        if (result.authTokens && result.authTokens.access_token) {
          const isExpired = isTokenExpired(result.authTokens.access_token);
          if (isExpired) {
            chrome.storage.local.remove('authTokens');
            setUser({});
            setLoggedIn(false);
          } else {
            setUser(result.authTokens);
            setLoggedIn(true);
          }
        }
      });
    } else {
      // we are not in the extension
      const authTokenStr = localStorage.getItem('authTokens');
      if (authTokenStr) {
        const authTokens = JSON.parse(authTokenStr);
        const isExpired = isTokenExpired(authTokens.access_token)
        if (isExpired) {
          localStorage.removeItem('authTokens');
          setUser({});
          setLoggedIn(false);
        } else {
          setUser(authTokens);
          setLoggedIn(true);
        }
      } else {
        setUser({});
        setLoggedIn(false);
      }
    }
    
  }, []);

  const exchangeCodeForToken = async (code: string) => {
    const tokenEndpoint = `${settings.cognitoDomain}/oauth2/token`
    const params = new URLSearchParams();
    params.append('grant_type', 'authorization_code');
    params.append('client_id', `${settings.clientId}`);
    if (chrome.runtime) {
      params.append('redirect_uri', `https://${chrome.runtime.id}.chromiumapp.org/`);
    } else {
      params.append('redirect_uri', `http://localhost:3000/`);

    }
    params.append('code', code);
  
    try {
      const response = await fetch(tokenEndpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded'
        }, body: params
      });
  
      if (!response.ok) {
        throw new Error(`HTTP ERROR! status: ${response.status}`);
      }
      const data = await response.json();
      if (chrome.storage) {
        chrome.storage.local.set({ authTokens: data });
      } else {
        localStorage.setItem('authTokens', JSON.stringify(data));
      }
      setUser(data);
      setLoggedIn(true);
    } catch (error) {
      console.error('error exchanging code for token', error);
      //throw error;
    }
  }

  const login = async () => {
    console.log("start auth flow");

    if (chrome.identity) {
      const cognitoUrl = `${settings.cognitoDomain}/login?response_type=code&client_id=${settings.clientId}&redirect_uri=https://${chrome.runtime.id}.chromiumapp.org/&scope=email+openid+profile`;
      const redirectURL = await chrome.identity.launchWebAuthFlow({
        url: cognitoUrl,
        interactive: true
      });
      if (redirectURL) {
        let url = new URL(redirectURL);
        let authorizationCode = url.searchParams.get("code");
        if (authorizationCode) exchangeCodeForToken(authorizationCode);
        else console.error("No authorization code in redirect url.");
      } else {
        console.error("Error with login.");
      }
    } else {
      const cognitoUrl = `${settings.cognitoDomain}/login?response_type=code&client_id=${settings.clientId}&redirect_uri=http://localhost:3000/&scope=email+openid+profile`;
      window.location.href = cognitoUrl;
    }
  }

  const logout = () => {
    setUser({});
    setLoggedIn(false);
  }

  return (
    <UserContext.Provider value={{ user, login, logout, exchangeCodeForToken, loggedIn }}>
      {children}
    </UserContext.Provider>
  );
}
export function useUserContext() { 
  return useContext(UserContext);
}
export default UserProvider;
