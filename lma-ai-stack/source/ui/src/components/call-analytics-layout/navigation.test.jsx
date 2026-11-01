/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import { NAV_PATHS } from './navigation';
import { generateNavigationItems } from '../common/navigation-items';
import {
  ASR_CONFIG_PATH,
  CONFIGURATION_PATH,
  DESKTOP_CAPTURE_APP_PATH,
  NOVA_SONIC_CONFIG_PATH,
  TRANSCRIPT_SUMMARY_PATH,
  VOICE_ENROLLMENT_PATH,
} from '../../routes/constants';

// Pages whose route deliberately renders no side navigation.
const NO_SIDE_NAV = new Set([DESKTOP_CAPTURE_APP_PATH, VOICE_ENROLLMENT_PATH]);

describe('side navigation visibility', () => {
  it('renders on every internal page it links to', () => {
    // A configuration page once shipped with an empty navigation panel: its route mounted
    // <Navigation />, but the component returns null off NAV_PATHS, and only the
    // activeHref branch had been added for the new path. Any admin nav link that
    // is an internal route must also be a path the navigation renders on.
    const links = generateNavigationItems({ AsrEngineAvailable: 'true', VoiceEnrollmentAvailable: 'true' }, true)
      .flatMap((entry) => (entry.type === 'section' ? entry.items : [entry]))
      .filter((item) => item.type === 'link' && item.href.startsWith('#/') && !item.external)
      .map((item) => item.href.slice(1));

    const missing = links.filter((path) => !NO_SIDE_NAV.has(path) && !NAV_PATHS.includes(path));
    expect(missing).toEqual([]);
  });

  it('offers one Configuration page to admins, in place of the pages for single sections', () => {
    const hrefs = (settings, isAdmin = true) =>
      generateNavigationItems(settings, isAdmin)
        .flatMap((entry) => (entry.type === 'section' ? entry.items : [entry]))
        .filter((item) => item.type === 'link')
        .map((item) => item.href.slice(1));

    expect(hrefs({ AsrEngineAvailable: 'true' })).toContain(CONFIGURATION_PATH);
    expect(hrefs({}, false)).not.toContain(CONFIGURATION_PATH);
    // Those paths still open, as a redirect to their section of the Configuration page.
    [ASR_CONFIG_PATH, NOVA_SONIC_CONFIG_PATH, TRANSCRIPT_SUMMARY_PATH].forEach((path) => {
      expect(hrefs({ AsrEngineAvailable: 'true' })).not.toContain(path);
    });
  });

  it('offers the enrollment portal only where the deployment stores voiceprints', () => {
    const hrefs = (settings, isAdmin = true) =>
      generateNavigationItems(settings, isAdmin)
        .flatMap((entry) => (entry.type === 'section' ? entry.items : [entry]))
        .filter((item) => item.type === 'link')
        .map((item) => item.href.slice(1));

    expect(hrefs({ VoiceEnrollmentAvailable: 'true' })).toContain(VOICE_ENROLLMENT_PATH);
    expect(hrefs({})).not.toContain(VOICE_ENROLLMENT_PATH);
    // Enrolling your own voice is self-service, not an admin task: only the
    // person whose voice it is can consent to it being stored.
    expect(hrefs({ VoiceEnrollmentAvailable: 'true' }, false)).toContain(VOICE_ENROLLMENT_PATH);
  });
});
