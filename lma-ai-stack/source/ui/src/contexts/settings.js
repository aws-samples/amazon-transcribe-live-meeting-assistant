/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useContext, createContext } from 'react';

export const SettingsContext = createContext(null);

const useSettingsContext = () => useContext(SettingsContext);

export default useSettingsContext;
