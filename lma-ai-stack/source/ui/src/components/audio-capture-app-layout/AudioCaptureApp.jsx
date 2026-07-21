/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useMemo } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  ExpandableSection,
  Header,
  Link,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';

import { LMA_VERSION } from '../common/constants';
import useSettingsContext from '../../contexts/settings';
import { STREAM_AUDIO_PATH, BROWSER_EXTENSION_PATH, VIRTUAL_PARTICIPANT_PATH } from '../../routes/constants';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

// Detect the user's OS so we can highlight the relevant platform. Purely a
// presentation hint — all platforms are listed regardless.
const detectOS = () => {
  try {
    const ua = (navigator.userAgent || '').toLowerCase();
    if (ua.includes('mac')) return 'mac';
    if (ua.includes('win')) return 'windows';
    if (ua.includes('android')) return 'android';
    if (/iphone|ipad|ipod/.test(ua)) return 'ios';
    return 'other';
  } catch (e) {
    return 'other';
  }
};

const resolveVersion = (settings) => {
  const raw = settings?.Version || LMA_VERSION;
  if (!raw || raw.includes('<VERSION_TOKEN>')) return null;
  return raw.startsWith('v') ? raw : `v${raw}`;
};

// Platform catalog. macOS is available today; the others are planned and shown
// as "Coming soon" so the roadmap is visible without implying they work yet.
const PLATFORMS = [
  {
    key: 'mac',
    name: 'macOS',
    status: 'available',
    note: 'Captures native app audio via ScreenCaptureKit + your microphone. Requires macOS 13 (Ventura) or later.',
  },
  { key: 'windows', name: 'Windows', status: 'planned', note: 'WASAPI loopback capture. Planned.' },
  {
    key: 'ios',
    name: 'iPhone / iPad',
    status: 'planned',
    note: 'ReplayKit / broadcast-upload capture. Under consideration.',
  },
  { key: 'android', name: 'Android', status: 'planned', note: 'AudioPlaybackCapture API. Under consideration.' },
];

// Audio Capture App vs Virtual Participant — an honest side-by-side so users
// pick the right tool. "app" = this Audio Capture App; "vp" = Virtual Participant.
const COMPARISON = [
  {
    dimension: 'How it captures',
    app: 'Runs on your Mac; captures OS system audio + your mic locally.',
    vp: 'A headless bot joins the meeting in the cloud and captures from inside it.',
  },
  {
    dimension: 'Meeting platforms',
    app: 'Any native or web app that plays audio on your Mac (Zoom, Teams, Webex, Slack, phone bridges, etc.).',
    vp: 'Only supported platforms it can automate (Zoom, Teams, Chime, Webex, Meet).',
  },
  {
    dimension: 'Speaker identification',
    app: 'No per-speaker names. Remote participants share one "Meeting Audio" channel; your mic is the "My Mic" one.',
    vp: 'Yes — tracks the active speaker names reported by the meeting platform.',
    appCon: true,
  },
  {
    dimension: 'In-meeting voice assistant',
    app: 'No. The Meeting Assistant is available in the LMA web UI (chat) only; it cannot speak into the meeting.',
    vp: 'Yes — optional Nova Sonic voice assistant can listen and speak in the meeting.',
    appCon: true,
  },
  {
    dimension: 'Visible to others',
    app: 'No bot or extra attendee — nothing appears in the participant list.',
    vp: 'A participant/bot joins the meeting and is visible to attendees.',
    appPro: true,
  },
  {
    dimension: 'Who must be present',
    app: 'You must be in the meeting with the app running on your Mac.',
    vp: 'Runs unattended in the cloud; you need not be present.',
    vpPro: true,
  },
  {
    dimension: 'Video / screen recording',
    app: 'Audio only.',
    vp: 'Can also capture the meeting screen/video recording.',
    appCon: true,
  },
];

// Defined at module scope (not inside the component) so React sees stable
// component types — satisfies react/no-unstable-nested-components and matches
// the codebase pattern of externalized table column configs.
const COMPARISON_COLUMNS = [
  {
    id: 'dimension',
    header: '',
    cell: (e) => <Box variant="strong">{e.dimension}</Box>,
    width: 190,
  },
  {
    id: 'app',
    header: 'Audio Capture App (this tool)',
    cell: (e) => (
      <Box>
        {e.appPro && <Badge color="green">Pro</Badge>}
        {e.appCon && <Badge color="grey">Limitation</Badge>} {e.app}
      </Box>
    ),
  },
  {
    id: 'vp',
    header: 'Virtual Participant',
    cell: (e) => (
      <Box>
        {e.vpPro && <Badge color="green">Pro</Badge>} {e.vp}
      </Box>
    ),
  },
];

const AudioCaptureApp = () => {
  const { settings } = useSettingsContext() || {};
  const version = useMemo(() => resolveVersion(settings), [settings]);
  const os = useMemo(() => detectOS(), []);

  // The macOS download URL is published by the audio-capture-app stack and
  // surfaced in settings as AudioCaptureAppDownloadUrl. Fall back to the
  // conventional versioned filename served from the web root if absent.
  const macZipName = version ? `lma-audio-capture-app-${version}.zip` : 'lma-audio-capture-app.zip';
  const macDownloadHref = settings?.AudioCaptureAppDownloadUrl || `/${macZipName}`;

  const copyToClipboard = (text) => {
    try {
      navigator.clipboard.writeText(text);
    } catch (e) {
      // no-op
    }
  };

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description="Transcribe meetings you join from a native desktop app — no browser tab or bot required."
            actions={
              <Button
                variant="primary"
                iconName="download"
                href={macDownloadHref}
                download={settings?.AudioCaptureAppDownloadUrl ? undefined : macZipName}
                target="_blank"
                rel="noopener noreferrer"
              >
                {version ? `Download for macOS (${version})` : 'Download for macOS'}
              </Button>
            }
          >
            Audio Capture App
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            The LMA Audio Capture App is a lightweight native application that streams your microphone and your
            computer&apos;s system (meeting) audio directly to LMA. Because it captures the operating system&apos;s
            audio &mdash; not a browser tab &mdash; it can transcribe meetings you join from a{' '}
            <strong>native Zoom, Teams, Webex, Slack, or phone-bridge app</strong>, which the{' '}
            <Link href={`#${BROWSER_EXTENSION_PATH}`}>Chrome Extension</Link> and{' '}
            <Link href={`#${STREAM_AUDIO_PATH}`}>Stream Audio</Link> options cannot. It adds no bot or extra attendee to
            the meeting.
          </Box>
          <Alert type="info" header="Sign in with your LMA account">
            The download is preconfigured for this LMA deployment. On first launch it asks for the same username and
            password you use for this web app &mdash; no tokens or endpoints to copy.
          </Alert>
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Choose your platform</Header>}>
        <SpaceBetween size="m">
          {PLATFORMS.map((p) => (
            <Box key={p.key} padding={{ vertical: 'xs' }}>
              <SpaceBetween direction="horizontal" size="s">
                <Box variant="strong">{p.name}</Box>
                {p.status === 'available' ? (
                  <Badge color="green">Available</Badge>
                ) : (
                  <Badge color="grey">Coming soon</Badge>
                )}
                {p.key === os && p.status === 'available' && <Badge color="blue">Your system</Badge>}
              </SpaceBetween>
              <Box variant="small" color="text-body-secondary">
                {p.note}
              </Box>
            </Box>
          ))}
          {os !== 'mac' && (
            <Alert type="warning" header="macOS is the only platform available today">
              You appear to be on a non-macOS system. The Windows and mobile apps are planned but not yet available. In
              the meantime, use the <Link href={`#${BROWSER_EXTENSION_PATH}`}>Chrome Extension</Link>,{' '}
              <Link href={`#${STREAM_AUDIO_PATH}`}>Stream Audio</Link>, or{' '}
              <Link href={`#${VIRTUAL_PARTICIPANT_PATH}`}>Virtual Participant</Link>.
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Install on macOS</Header>}>
        <SpaceBetween size="m">
          <Box variant="p">
            The app is distributed as source that you build on your Mac with a single script. (A native macOS app using
            ScreenCaptureKit cannot be prebuilt by the LMA cloud pipeline, and Apple&apos;s signing tools are
            macOS-only, so building locally is both required and the most trustworthy option &mdash; nothing to
            un-quarantine.)
          </Box>
          <ol>
            <li>
              <Box variant="p">
                Click <strong>Download for macOS</strong> above to get <code>{macZipName}</code>, then unzip it.
              </Box>
            </li>
            <li>
              <Box variant="p">
                If you don&apos;t already have Apple&apos;s command-line tools, install them (one time):{' '}
                <code>xcode-select --install</code>{' '}
                <Button
                  variant="inline-icon"
                  iconName="copy"
                  ariaLabel="Copy xcode-select --install"
                  onClick={() => copyToClipboard('xcode-select --install')}
                />
              </Box>
            </li>
            <li>
              <Box variant="p">
                In Terminal, <code>cd</code> into the unzipped folder and run the installer:{' '}
                <code>./install-macos.sh</code>{' '}
                <Button
                  variant="inline-icon"
                  iconName="copy"
                  ariaLabel="Copy ./install-macos.sh"
                  onClick={() => copyToClipboard('./install-macos.sh')}
                />
                . It checks prerequisites, builds the app, and bundles it as <code>LMAAudioClient.app</code>.
              </Box>
            </li>
            <li>
              <Box variant="p">
                Launch the app. macOS prompts for <strong>Microphone</strong> access &mdash; approve it. Then open{' '}
                <strong>System Settings &rsaquo; Privacy &amp; Security &rsaquo; Screen Recording</strong>, enable{' '}
                <strong>LMA Audio Client</strong>, and relaunch (Screen Recording requires a restart to take effect).
                Screen Recording is what lets macOS capture system/meeting audio.
              </Box>
            </li>
            <li>
              <Box variant="p">
                Enter your LMA username and password when prompted. The app signs in, starts streaming, and your meeting
                appears in the <Link href="#/calls">Meetings List</Link> with a live transcript.
              </Box>
            </li>
          </ol>
          <Alert type="info" header="Tip: use headphones">
            For the cleanest transcript, wear headphones. Otherwise your speakers&apos; meeting audio can bleed into
            your microphone and appear faintly on both transcript channels.
          </Alert>
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">How it works</Header>}>
        <SpaceBetween size="s">
          <ul>
            <li>Your microphone is transcribed as the meeting owner (the &quot;My Mic&quot; channel).</li>
            <li>
              Your computer&apos;s system audio &mdash; the remote participants &mdash; is the &quot;Meeting Audio&quot;
              channel.
            </li>
            <li>Audio streams to the same secure endpoint the browser options use; the server is unchanged.</li>
            <li>
              The app lives in your menu bar; the icon turns red while recording. Click Stop to end the meeting, which
              then finalizes in your Meetings List.
            </li>
          </ul>
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Running it in the background</Header>}>
        <SpaceBetween size="s">
          <Box variant="p">
            The app is a menu-bar app (no Dock icon). It uses no audio or CPU when idle, so the intended usage is to
            leave it running and click <strong>Start</strong> when a meeting begins.
          </Box>
          <ul>
            <li>
              <strong>Left-click</strong> the <strong>LMA</strong> menu-bar item (top-right) for controls.{' '}
              <strong>Right-click</strong> it for <strong>Quit</strong> (kept out of the popover so it isn&apos;t
              confused with Stop).
            </li>
            <li>
              <strong>Start automatically at login:</strong> move the app to <code>/Applications</code>, then turn on
              the <strong>Start automatically at login</strong> toggle in the popover (or System Settings &rsaquo;
              General &rsaquo; Login Items).
            </li>
            <li>
              <strong>Relaunch after quitting:</strong> press <strong>⌘-Space</strong>, type{' '}
              <strong>LMA Audio Client</strong>, and press Return &mdash; or run <code>open -a LMAAudioClient</code>.
            </li>
          </ul>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header variant="h2" description="Both transcribe without a browser tab, but make different trade-offs.">
            Audio Capture App vs Virtual Participant
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Table
            variant="embedded"
            columnDefinitions={COMPARISON_COLUMNS}
            items={COMPARISON}
            wrapLines
            ariaLabels={{ tableLabel: 'Audio Capture App versus Virtual Participant comparison' }}
          />
          <Box variant="small" color="text-body-secondary">
            In short: choose the <strong>Audio Capture App</strong> when you&apos;re attending yourself, want no visible
            bot, need a platform the Virtual Participant doesn&apos;t support, or prefer to keep audio on your machine.
            Choose the <Link href={`#${VIRTUAL_PARTICIPANT_PATH}`}>Virtual Participant</Link> when you need per-speaker
            names, an in-meeting voice assistant, screen/video capture, or hands-off unattended recording.
          </Box>
        </SpaceBetween>
      </Container>

      <ExpandableSection headerText="Troubleshooting">
        <SpaceBetween size="s">
          <Box variant="p">
            <strong>&quot;Apple could not verify … is free of malware&quot; (Gatekeeper).</strong> macOS flags files
            downloaded from a browser. The installer clears this automatically. If you hit the warning first, clear it
            from the unzipped folder and re-run: <code>xattr -dr com.apple.quarantine .</code>
            <Button
              variant="inline-icon"
              iconName="copy"
              ariaLabel="Copy xattr command"
              onClick={() => copyToClipboard('xattr -dr com.apple.quarantine .')}
            />
          </Box>
          <Box variant="p">
            <strong>&quot;xcode-select: command not found&quot; or build errors.</strong> Install Apple&apos;s
            command-line tools with <code>xcode-select --install</code>, complete the popup, and re-run{' '}
            <code>./install-macos.sh</code>.
          </Box>
          <Box variant="p">
            <strong>No remote-participant audio in the transcript.</strong> Grant <strong>Screen Recording</strong> to
            &quot;LMA Audio Client&quot; in System Settings and relaunch. Audio-only capture still requires the Screen
            Recording permission on macOS.
          </Box>
          <Box variant="p">
            <strong>Sign-in fails.</strong> Use the same email and password you use for this LMA web app. If your
            organization uses SSO, this app&apos;s username/password sign-in may not apply &mdash; use the{' '}
            <Link href={`#${BROWSER_EXTENSION_PATH}`}>Chrome Extension</Link> instead.
          </Box>
          <Box variant="p">
            For more help, see the{' '}
            <Link href={`${DOCS_BASE}/`} external target="_blank">
              LMA documentation
            </Link>
            .
          </Box>
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  );
};

export default AudioCaptureApp;
