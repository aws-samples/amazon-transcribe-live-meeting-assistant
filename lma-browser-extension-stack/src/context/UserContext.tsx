/* eslint-disable @typescript-eslint/no-empty-function */
import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { useSettings } from './SettingsContext';

type User = {
  id_token?: string,
  access_token?: string,
  refresh_token?: string
};

const initialUserContext = {
  user: {} as User,
  login: () => { },
  logout: () => { },
  exchangeCodeForToken: async (codeOrToken: string, grantType: string) => { return true; },
  loggedIn: false,
  checkTokenExpired: async (user: User) => { return true; }
};
const UserContext = createContext(initialUserContext);

function UserProvider({ children }: any) {
  const [user, setUser] = useState<User>({});
  const settings = useSettings();
  const [loggedIn, setLoggedIn] = useState(false);

  const isTokenExpired = (jwtToken: string) => {
    const [, payload] = jwtToken.split('.');
    const { exp: expires } = JSON.parse(atob(payload));
    if (typeof expires === 'number') {
      const expiryDate = new Date(expires * 1000);
      console.log("expiry:", expiryDate);
      return (expiryDate < new Date());
    }
    return true;
  }

  const shouldForceLogout = (jwtToken: string) => {
    const [, payload] = jwtToken.split('.');
    const { exp: expires } = JSON.parse(atob(payload));
    if (typeof expires === 'number') {
      const expiryDate = new Date(expires * 1000);
      const now = new Date();
      // Force logout if token expires in less than 5 minutes
      return (expiryDate.getTime() - now.getTime()) < 5 * 60 * 1000;
    }
    return true;
  }

  const logout = useCallback(() => {
    if (chrome.storage) {
      chrome.storage.local.remove('authTokens');
    } else {
      localStorage.removeItem('authTokens');
    }
    setUser({});
    setLoggedIn(false);
  }, []);

  const exchangeCodeForToken = useCallback(async (codeOrToken: string, grantType: string) => {
    const tokenEndpoint = `${settings.cognitoDomain}/oauth2/token`
    const params = new URLSearchParams();

    params.append('grant_type', grantType);
    params.append((grantType === 'authorization_code' ? 'code' : 'refresh_token'), codeOrToken);
    params.append('client_id', `${settings.clientId}`);

    if (chrome.runtime) {
      params.append('redirect_uri', `https://${chrome.runtime.id}.chromiumapp.org/`);
    } else {
      params.append('redirect_uri', `http://localhost:3000/`);
    }

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
      return true;
    } catch (error) {
      console.error('error exchanging code for token', error);
      return false;
    }
  }, [settings]);

  const login = useCallback(async () => {
    console.log("start auth flow");
    try {
      if (chrome.identity) {
        const cognitoUrl = `${settings.cognitoDomain}/oauth2/authorize?response_type=code&client_id=${settings.clientId}&redirect_uri=https://${chrome.runtime.id}.chromiumapp.org/&scope=email+openid+profile`;
        const redirectURL = await chrome.identity.launchWebAuthFlow({
          url: cognitoUrl,
          interactive: true
        });
        if (redirectURL) {
          const url = new URL(redirectURL);
          console.log(redirectURL)
          const authorizationCode = url.searchParams.get("code");
          if (authorizationCode) exchangeCodeForToken(authorizationCode, 'authorization_code');
          else console.error("No authorization code in redirect url.");
        } else {
          console.error("Error with login.");
        }
      } else {
        const cognitoUrl = `${settings.cognitoDomain}/oauth2/authorize?response_type=code&client_id=${settings.clientId}&redirect_uri=http://localhost:3000/&scope=email+openid+profile`;
        window.location.href = cognitoUrl;
      }
    } catch (error) {
      console.error('error logging in BROWSER EXTENSION', error);
    }
  }, [exchangeCodeForToken, settings]);

  const checkTokenAndLogout = useCallback(() => {
    if (!user.access_token) {
      return;
    }

    if (shouldForceLogout(user.access_token)) {
      console.log("Token expiring soon, forcing logout");
      logout();
    }
  }, [user, logout]);

  // Set up periodic token check
  useEffect(() => {
    if (loggedIn && user.access_token) {
      // Check token every minute
      const intervalId = setInterval(checkTokenAndLogout, 60 * 1000);
      return () => clearInterval(intervalId);
    }
  }, [loggedIn, user, checkTokenAndLogout]);

  const refreshTokenIfNeeded = useCallback(async () => {
    if (!user.access_token || !user.refresh_token) {
      return;
    }

    if (shouldRefreshToken(user.access_token)) {
      console.log("Proactively refreshing token");
      const refreshed = await exchangeCodeForToken(user.refresh_token, 'refresh_token');
      if (!refreshed) {
        console.error("Failed to refresh token");
        logout();
      }
    }
  }, [user, exchangeCodeForToken, logout]);

  // Set up periodic token refresh check
  useEffect(() => {
    if (loggedIn && user.access_token) {
      // Check token every minute
      const intervalId = setInterval(refreshTokenIfNeeded, 60 * 1000);
      return () => clearInterval(intervalId);
    }
  }, [loggedIn, user, refreshTokenIfNeeded]);

  const checkTokenExpired = async (user: User) => {
    if (!(user && user.access_token)) {
      console.log("checkTokenExpired: no user token set")
      return true;
    }
    const isExpired = isTokenExpired(user.access_token);
    if (!isExpired) {
      console.log("checkTokenExpired: token not expired")
      return false;
    }
    if (user.refresh_token) {
      console.log("checkTokenExpired: refreshing expired token");
      const refreshed = await exchangeCodeForToken(user.refresh_token, 'refresh_token');
      if (refreshed) {
        return false;
      }
    }
    console.log("checkTokenExpired: unable to refresh expired token");
    return true;
  };

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
            // try to refresh anyway
            exchangeCodeForToken(result.authTokens.refresh_token, 'refresh_token');
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
          logout();
        } else {
          setUser(authTokens);
          setLoggedIn(true);
        }
      } else {
        logout();
      }
    }

  }, []);

  return (
    <UserContext.Provider value={{ user, login, logout, exchangeCodeForToken, loggedIn, checkTokenExpired }}>
      {children}
    </UserContext.Provider>
  );
}
export function useUserContext() {
  return useContext(UserContext);
}
export default UserProvider;
