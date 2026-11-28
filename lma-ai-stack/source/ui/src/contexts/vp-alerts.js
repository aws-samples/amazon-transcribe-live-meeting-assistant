/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useContext, createContext } from 'react';

/**
 * The Flashbar items for the signed-in user's Virtual Participant alerts.
 *
 * Provided once, by ManualActionAlerts at the app shell, so the alerts survive
 * moving between pages; read by useNotifications, so every page shows them in
 * its own AppLayout's notifications slot, below the top navigation. Empty
 * outside the provider (a Participant's pages mount none).
 */
export const VpAlertsContext = createContext([]);

const useVpAlerts = () => useContext(VpAlertsContext);

export default useVpAlerts;
