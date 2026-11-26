/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meetings layout's drawers, rendered in Cloudscape's own AppLayout: a
 * meeting's first notification, arriving while its page is open, opens the
 * Notifications drawer; Help stays where it was when that drawer is added.
 */
import React, { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { AppLayout } from '@cloudscape-design/components';

import layoutDrawerProps, { TOOLS_DRAWER_ID } from './layout-drawers';
import useNotificationsDrawer, { NOTIFICATIONS_DRAWER_ID } from '../../hooks/use-notifications-drawer';

const memoryStorage = () => {
  const values = {};
  return {
    getItem: (key) => values[key] ?? null,
    setItem: (key, value) => {
      values[key] = value;
    },
  };
};

/** The layout as CallAnalyticsLayout builds it, for one meeting whose notifications arrive by `arrive`. */
const renderLayout = ({ callId = 'm1', initialItems = 0, helpOpen = false } = {}) => {
  const controls = {};
  const Harness = () => {
    const [items, setItems] = useState(initialItems);
    const [arrivals, setArrivals] = useState(0);
    const [toolsOpen, setToolsOpen] = useState(helpOpen);
    controls.arrive = () => {
      setItems((count) => count + 1);
      setArrivals((count) => count + 1);
    };
    const storage = React.useMemo(memoryStorage, []);
    const drawer = useNotificationsDrawer({ callId, liveArrivals: arrivals, covered: toolsOpen, storage });
    const props = layoutDrawerProps({
      drawer,
      showNotifications: Boolean(callId) && items > 0,
      visibleCount: items,
      notificationsContent: <div>notifications content</div>,
      toolsContent: <div>help content</div>,
      toolsOpen,
      setToolsOpen,
    });
    controls.props = props;
    controls.drawer = drawer;
    // eslint-disable-next-line react/jsx-props-no-spreading
    return <AppLayout content={<div>page</div>} {...props} />;
  };
  render(<Harness />);
  return controls;
};

describe('layoutDrawerProps', () => {
  it('offers Help as a drawer on every page, never as the tools panel', () => {
    const drawer = { open: false, badge: false, size: 360, setOpen: () => {}, hide: () => {}, resize: () => {} };
    const base = { drawer, visibleCount: 0, notificationsContent: null, toolsContent: null, setToolsOpen: () => {} };
    const listPage = layoutDrawerProps({ ...base, showNotifications: false, toolsOpen: false });
    expect(listPage).not.toHaveProperty('tools');
    expect(listPage.drawers.map((d) => d.id)).toEqual([TOOLS_DRAWER_ID]);
    const meetingPage = layoutDrawerProps({ ...base, showNotifications: true, toolsOpen: false });
    expect(meetingPage).not.toHaveProperty('tools');
    expect(meetingPage.drawers.map((d) => d.id)).toEqual([NOTIFICATIONS_DRAWER_ID, TOOLS_DRAWER_ID]);
  });

  it("opens the Notifications drawer for a meeting's first notification", () => {
    const layout = renderLayout();
    expect(screen.queryByText('notifications content')).toBeNull();
    act(() => layout.arrive());
    expect(layout.props.activeDrawerId).toBe(NOTIFICATIONS_DRAWER_ID);
    expect(screen.getByText('notifications content')).toBeTruthy();
  });

  it('opens it for a later notification on a meeting that already had some', () => {
    const layout = renderLayout({ initialItems: 2 });
    expect(screen.queryByText('notifications content')).toBeNull();
    act(() => layout.arrive());
    expect(screen.getByText('notifications content')).toBeTruthy();
  });

  it('keeps Help open when the first notification arrives, and marks the Notifications trigger', () => {
    const layout = renderLayout({ helpOpen: true });
    expect(screen.getByText('help content')).toBeTruthy();
    act(() => layout.arrive());
    expect(screen.getByText('help content')).toBeTruthy();
    expect(layout.drawer.badge).toBe(true);
    expect(layout.props.activeDrawerId).toBe(TOOLS_DRAWER_ID);
  });

  it('opens Help on a page with no notifications', () => {
    const layout = renderLayout({ callId: null });
    act(() => layout.props.onDrawerChange({ detail: { activeDrawerId: TOOLS_DRAWER_ID } }));
    expect(layout.props.activeDrawerId).toBe(TOOLS_DRAWER_ID);
    expect(screen.getByText('help content')).toBeTruthy();
  });
});
