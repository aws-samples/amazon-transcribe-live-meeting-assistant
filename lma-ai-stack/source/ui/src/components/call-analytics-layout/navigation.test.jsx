/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { NAV_PATHS } from './navigation';
import { generateNavigationItems } from '../common/navigation-items';
import { DESKTOP_CAPTURE_APP_PATH } from '../../routes/constants';

// Pages whose route deliberately renders no side navigation.
const NO_SIDE_NAV = new Set([DESKTOP_CAPTURE_APP_PATH]);

describe('side navigation visibility', () => {
  it('renders on every internal page it links to', () => {
    // The Transcription Engine page shipped with an empty navigation panel: its route mounted
    // <Navigation />, but the component returns null off NAV_PATHS, and only the
    // activeHref branch had been added for the new path. Any admin nav link that
    // is an internal route must also be a path the navigation renders on.
    const links = generateNavigationItems({}, true)
      .flatMap((entry) => (entry.type === 'section' ? entry.items : [entry]))
      .filter((item) => item.type === 'link' && item.href.startsWith('#/') && !item.external)
      .map((item) => item.href.slice(1));

    const missing = links.filter((path) => !NO_SIDE_NAV.has(path) && !NAV_PATHS.includes(path));
    expect(missing).toEqual([]);
  });
});
