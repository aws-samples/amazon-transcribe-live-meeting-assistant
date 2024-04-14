/* eslint-disable @typescript-eslint/no-empty-function */
import React, { createContext, useContext, useState } from 'react';

const initialNavigationState = {
  currentScreen: 'login',
  navigate: (screen:string) => { }
};

const NavigationContext = createContext(initialNavigationState);

function NavigationProvider({ children }:any) {
  const [currentScreen, setCurrentScreen] = useState(initialNavigationState.currentScreen);

  const navigate = (screen: string) => {
    console.log('navigate');
    setCurrentScreen(screen);
  }

  return (
    <NavigationContext.Provider value={{ currentScreen, navigate }}>
      {children}
    </NavigationContext.Provider>
  );
}
export function useNavigation() { 
  return useContext(NavigationContext);
}
export default NavigationProvider;
