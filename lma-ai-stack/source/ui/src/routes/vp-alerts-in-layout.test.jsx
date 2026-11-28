/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Virtual Participant alerts are shown in each page's AppLayout notifications
 * slot, below the top navigation, and never as an overlay above the header.
 */
import React from 'react';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

const { subscribers, graphql, bellClicks } = vi.hoisted(() => ({
  subscribers: [],
  graphql: vi.fn(),
  bellClicks: { count: 0 },
}));

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('aws-amplify/utils', () => ({ ConsoleLogger: class {} }));
vi.mock('../contexts/app', () => ({
  default: () => ({ navigationOpen: false, setNavigationOpen: () => {}, errorMessage: '', setErrorMessage: () => {} }),
}));
// The real top navigation, reduced to what this test needs: the element the
// layout's headerSelector names, holding the notifications bell.
vi.mock('../components/call-analytics-top-navigation', () => ({
  default: () => (
    <div id="top-navigation" data-testid="top-navigation">
      <button
        type="button"
        onClick={() => {
          bellClicks.count += 1;
        }}
      >
        Meeting notifications
      </button>
    </div>
  ),
}));
vi.mock('../components/call-analytics-layout/navigation', () => ({ default: () => null }));
vi.mock('../components/mcp-servers-page/breadcrumbs', () => ({ default: () => null }));
vi.mock('../components/mcp-servers-page/tools-panel', () => ({ default: () => null }));
vi.mock('../components/mcp-servers-page/MCPServersPage', () => ({ default: () => <div>page content</div> }));

/* eslint-disable import/first */
import ManualActionAlerts from '../components/virtual-participant-layout/ManualActionAlerts';
import MCPServersRoutes from './MCPServersRoutes';
/* eslint-enable import/first */

const FAILED_VP = {
  id: 'vp-9',
  meetingName: 'Quarterly review',
  meetingPlatform: 'ZOOM',
  status: 'FAILED',
  errorMessage: 'The meeting ended before the participant was admitted',
  updatedAt: '2026-11-28T10:00:00.000Z',
};

const emit = (vp) =>
  act(() => {
    subscribers.forEach((next) => next({ data: { onUpdateVirtualParticipant: vp } }));
  });

/** Whether the element, or any ancestor, is laid out as a fixed overlay. */
const insideFixedOverlay = (element) => {
  for (let node = element; node; node = node.parentElement) {
    if (node.style?.position === 'fixed') return true;
  }
  return false;
};

describe('Virtual Participant alerts on a page', () => {
  beforeEach(async () => {
    subscribers.length = 0;
    bellClicks.count = 0;
    window.localStorage.clear();
    graphql.mockImplementation((operation) => {
      if (String(operation?.query).includes('subscription')) {
        return {
          subscribe: ({ next }) => {
            subscribers.push(next);
            return { unsubscribe: () => {} };
          },
        };
      }
      return Promise.resolve({ data: { listVirtualParticipants: [] } });
    });
    await act(async () => {
      render(
        <MemoryRouter>
          <ManualActionAlerts>
            <MCPServersRoutes />
          </ManualActionAlerts>
        </MemoryRouter>,
      );
    });
    expect(subscribers.length).toBeGreaterThan(0);
  });

  afterEach(() => {
    cleanup();
  });

  it('shows a failed Virtual Participant in the layout notifications, below the top navigation', () => {
    emit(FAILED_VP);

    const header = screen.getByText('Virtual participant failed — Quarterly review');
    const notifications = createWrapper().findAppLayout().findNotifications().getElement();
    expect(notifications.contains(header)).toBe(true);
    expect(screen.getByTestId('top-navigation').contains(header)).toBe(false);
    expect(insideFixedOverlay(header)).toBe(false);
    // The notifications slot comes after the header in the document.
    const all = [...document.querySelectorAll('*')];
    expect(all.indexOf(notifications)).toBeGreaterThan(all.indexOf(screen.getByTestId('top-navigation')));
  });

  it('keeps the top navigation usable while the alert is up', () => {
    emit(FAILED_VP);

    fireEvent.click(screen.getByRole('button', { name: 'Meeting notifications' }));
    expect(bellClicks.count).toBe(1);
  });

  it('removes the alert from the layout when it is dismissed', async () => {
    emit(FAILED_VP);
    const flashbar = createWrapper().findAppLayout().findNotifications().findFlashbar();
    const item = flashbar.findItems().find((i) => i.getElement().textContent.includes('Quarterly review'));
    act(() => {
      item.findDismissButton().click();
    });
    // After the Flashbar's exit transition.
    await waitFor(() => expect(screen.queryByText('Virtual participant failed — Quarterly review')).toBeNull());
  });
});

// Every page layout takes its banners from useNotifications, which is where the
// Virtual Participant alerts join the page's own, and keeps them in view with
// stickyNotifications. A layout that built its notifications another way would
// silently show no Virtual Participant alerts.
const SRC = path.resolve(__dirname, '..');
const sourceFiles = (dir) =>
  readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return /\.jsx?$/.test(name) && !/\.test\./.test(name) ? [full] : [];
  });

describe('page layouts', () => {
  const layouts = sourceFiles(SRC).filter((file) => readFileSync(file, 'utf-8').includes('<AppLayout'));

  it('are found', () => {
    expect(layouts.length).toBeGreaterThan(10);
  });

  it.each(layouts.map((file) => [path.relative(SRC, file), file]))(
    '%s shows the shared notifications, sticky while a Virtual Participant alert is up',
    (_name, file) => {
      const text = readFileSync(file, 'utf-8');
      expect(text).toContain('notifications={<Flashbar items={notifications} />}');
      expect(text).toContain('useNotifications()');
      expect(text).toContain('stickyNotifications={stickyNotifications}');
    },
  );

  it('no source file draws a fixed overlay above the page', () => {
    const fixed = sourceFiles(SRC).filter((file) => /position:\s*'fixed'/.test(readFileSync(file, 'utf-8')));
    expect(fixed.map((file) => path.relative(SRC, file))).toEqual([]);
  });
});
