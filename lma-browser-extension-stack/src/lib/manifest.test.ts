/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Checks `public/manifest.json` against the hostnames the panel knows how to
 * name a platform for.
 *
 * These two lists are maintained in different files and in different languages,
 * and they have drifted before: `platformFromBaseUrl` gained
 * `teams.cloud.microsoft` while the manifest kept registering the Teams reader
 * for `teams.microsoft.com` alone, so on the newer host no reader loaded, and
 * the panel showed no platform, no name and no active speaker for a meeting it
 * was nonetheless capturing. Only the audio recorder, which is registered for
 * `<all_urls>`, kept working — which is why the failure is quiet.
 *
 * The checks run in both directions: a named host with no reader registered for
 * it, and a reader registered for hosts the panel cannot name. Both have
 * happened. What is not checked is the path component of a match pattern — the
 * Chime registration is scoped to `/meetings/*`, and a reader that stops loading
 * because the platform moved its meeting URLs would not show up here.
 *
 * The manifest is read from disk rather than imported so that this asserts
 * against the file the build actually ships.
 */

import fs from 'fs';
import path from 'path';
import { MEETING_PLATFORM_HOSTS, platformFromBaseUrl, UNKNOWN_PLATFORM } from './capture';

type ContentScript = {
  matches?: string[];
  js?: string[];
};

const manifest = JSON.parse(
  fs.readFileSync(path.join(__dirname, '..', '..', 'public', 'manifest.json'), 'utf8'),
);

const contentScripts: ContentScript[] = manifest.content_scripts || [];

/** The reader (not the recorder) registrations, keyed by their provider script. */
function readerFor(providerScript: string): ContentScript | undefined {
  return contentScripts.find(
    (script) => (script.js || []).indexOf(`content_scripts/providers/${providerScript}`) !== -1,
  );
}

/** Every host in every reader's match patterns, with the scheme and path removed. */
function registeredHosts(): string[] {
  return contentScripts
    .filter((script) => (script.js || []).some((js) => js.startsWith('content_scripts/providers/')))
    .reduce<string[]>((hosts, script) => hosts.concat(script.matches || []), [])
    .map((pattern) => pattern.replace(/^https?:\/\//, '').replace(/\/.*$/, ''));
}

/**
 * Does `pattern` (a manifest host pattern) cover `host`?
 *
 * Chrome's `*.example.com` matches example.com itself as well as any subdomain
 * of it, so a wildcard pattern covers a host at or below its base.
 */
function covers(pattern: string, host: string): boolean {
  if (pattern === host) {
    return true;
  }
  if (pattern.startsWith('*.')) {
    const base = pattern.slice(2);
    return host === base || host.endsWith(`.${base}`);
  }
  return false;
}

describe('manifest content scripts', () => {
  it('registers a reader for every host the panel can name a platform for', () => {
    const hosts = registeredHosts();
    const missing: string[] = [];
    MEETING_PLATFORM_HOSTS.forEach(({ exact = [], suffix = [] }) => {
      exact.concat(suffix).forEach((host) => {
        if (!hosts.some((pattern) => covers(pattern, host))) {
          missing.push(host);
        }
      });
    });
    expect(missing).toEqual([]);
  });

  it('registers a reader for subdomains of every host matched by suffix', () => {
    // A host declared as a suffix is named for its subdomains too, so a manifest
    // that registers only the base host leaves a meeting on a subdomain with a
    // platform label and no reader to produce one.
    const hosts = registeredHosts();
    const missing: string[] = [];
    MEETING_PLATFORM_HOSTS.forEach(({ suffix = [] }) => {
      suffix.forEach((host) => {
        const subdomain = `meet1234.${host}`;
        if (!hosts.some((pattern) => covers(pattern, subdomain))) {
          missing.push(subdomain);
        }
      });
    });
    expect(missing).toEqual([]);
  });

  it('names a platform for every host a reader is registered for', () => {
    // The other direction of the same invariant, and the one the Zoom half of
    // this change was about: the reader loaded on every `zoom.us` host while the
    // panel named the platform for one of them. A registration the panel cannot
    // name means a reader runs and the platform still reads "n/a".
    //
    // Checked per pattern rather than per host, because a wildcard pattern is
    // allowed to be broader than the named host it exists to cover: the Chime
    // registration is `*.chime.aws` while only `app.chime.aws` is named.
    const unnameable = registeredHosts().filter((pattern) => {
      const candidates = pattern.startsWith('*.')
        ? MEETING_PLATFORM_HOSTS
          .reduce<string[]>((all, { exact = [], suffix = [] }) => all.concat(exact, suffix), [])
          .filter((host) => covers(pattern, host))
        : [pattern];
      return !candidates.some((host) => platformFromBaseUrl(`https://${host}`) !== UNKNOWN_PLATFORM);
    });
    expect(unnameable).toEqual([]);
  });

  it('registers the Teams reader for both of the hosts Teams is served from', () => {
    // Called out separately from the loop above because this is the specific
    // regression: Microsoft serves Teams from the original host and from the
    // unified `cloud.microsoft` domain, and a tenant may land on either.
    const teams = readerFor('teams.js');
    expect(teams).toBeDefined();
    const matches = (teams as ContentScript).matches || [];
    expect(matches).toContain('https://teams.microsoft.com/*');
    expect(matches).toContain('https://teams.cloud.microsoft/*');
  });

  it('registers the audio recorder for all URLs, independently of any reader', () => {
    // The recorder has to keep working on a platform whose reader has gone
    // stale; this is what lets capture continue while metadata is blank.
    const recorder = contentScripts.find(
      (script) => (script.js || []).indexOf('content_scripts/recorder/inject-recorder.js') !== -1,
    );
    expect(recorder).toBeDefined();
    expect((recorder as ContentScript).matches).toContain('<all_urls>');
  });

  it('lists every provider script that a reader injects as web accessible', () => {
    // An injection script that is not web accessible cannot be loaded into the
    // page, and the reader that injects it fails silently.
    const resources: string[] = (manifest.web_accessible_resources || [])
      .reduce((all: string[], entry: { resources?: string[] }) => all.concat(entry.resources || []), []);
    expect(resources).toContain('content_scripts/providers/zoom-injection.js');
    expect(resources).toContain('content_scripts/recorder/recorder.js');
  });
});
