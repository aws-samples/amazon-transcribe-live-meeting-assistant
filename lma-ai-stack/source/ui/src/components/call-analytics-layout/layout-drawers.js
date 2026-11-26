/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { NOTIFICATIONS_DRAWER_ID } from '../../hooks/use-notifications-drawer';
import { appLayoutLabels } from '../common/labels';

/** The help panel's drawer, on every page of the meetings layout. */
export const TOOLS_DRAWER_ID = 'tools';

/**
 * The AppLayout props for the meetings layout's drawers: Help on every page,
 * and on a meeting page that has notifications, the Notifications drawer
 * before it (docs/meeting-notifications.md).
 *
 * The layout is always given `drawers`, never `tools`, even where Help is the
 * only drawer: Cloudscape's AppLayout does not show the drawer named by
 * `activeDrawerId` once it has been changed from `tools` to `drawers` after it
 * was first shown, whereas a drawer added to `drawers` is shown as expected.
 * So the Notifications drawer is added to the list when the meeting's first
 * notification arrives, and Help is named as the other layouts' tools trigger
 * is (appLayoutLabels).
 *
 * `drawer` is what useNotificationsDrawer returns; `showNotifications` is
 * whether the Notifications drawer is offered; `visibleCount` is its count of
 * notifications shown; `notificationsContent` and `toolsContent` are the two
 * drawers' contents.
 */
const layoutDrawerProps = ({
  drawer,
  showNotifications,
  visibleCount,
  notificationsContent,
  toolsContent,
  toolsOpen,
  setToolsOpen,
}) => {
  let activeDrawerId = null;
  if (toolsOpen) activeDrawerId = TOOLS_DRAWER_ID;
  else if (showNotifications && drawer.open) activeDrawerId = NOTIFICATIONS_DRAWER_ID;
  const toolsDrawer = {
    id: TOOLS_DRAWER_ID,
    content: toolsContent,
    trigger: { iconName: 'status-info' },
    ariaLabels: {
      drawerName: appLayoutLabels.tools,
      closeButton: appLayoutLabels.toolsClose,
      triggerButton: appLayoutLabels.toolsToggle,
    },
  };
  const notificationsDrawer = {
    id: NOTIFICATIONS_DRAWER_ID,
    content: notificationsContent,
    trigger: { iconName: 'notification' },
    badge: drawer.badge,
    resizable: true,
    defaultSize: drawer.size,
    onResize: ({ detail }) => drawer.resize(detail.size),
    ariaLabels: {
      drawerName: `Notifications (${visibleCount})`,
      closeButton: 'Close notifications',
      triggerButton: drawer.badge ? 'Notifications, has new notifications' : 'Notifications',
      resizeHandle: 'Resize notifications',
    },
  };
  return {
    drawers: showNotifications ? [notificationsDrawer, toolsDrawer] : [toolsDrawer],
    activeDrawerId,
    onDrawerChange: ({ detail }) => {
      setToolsOpen(detail.activeDrawerId === TOOLS_DRAWER_ID);
      if (!showNotifications) return;
      // Opening Help in its place is not closing it: only a close of the
      // Notifications drawer itself keeps it from opening by itself again.
      if (detail.activeDrawerId === NOTIFICATIONS_DRAWER_ID) drawer.setOpen(true);
      else if (activeDrawerId === NOTIFICATIONS_DRAWER_ID && detail.activeDrawerId === null) drawer.setOpen(false);
      else drawer.hide();
    },
  };
};

export default layoutDrawerProps;
