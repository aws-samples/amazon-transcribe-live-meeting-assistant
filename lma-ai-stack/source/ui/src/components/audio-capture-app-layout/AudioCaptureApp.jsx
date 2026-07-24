/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  ExpandableSection,
  Header,
  Link,
  SegmentedControl,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';

import { LMA_VERSION } from '../common/constants';
import useSettingsContext from '../../contexts/settings';
import { STREAM_AUDIO_PATH, BROWSER_EXTENSION_PATH, VIRTUAL_PARTICIPANT_PATH } from '../../routes/constants';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

// Detect the user's OS so we can DEFAULT the platform selector to it. Detection
// is only a default — the selector lets the user switch (e.g. browsing on
// Windows but building for their Mac, or a misfiring user-agent string).
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

// Platform catalog. Two states: 'available' (works today) and 'considering'
// (being evaluated, no commitment → "Under consideration"). macOS and Windows
// are available; mobile is under consideration.
const PLATFORMS = [
  {
    key: 'mac',
    name: 'macOS',
    status: 'available',
    note: 'Captures native app audio via ScreenCaptureKit + your microphone. Requires macOS 13 (Ventura) or later.',
  },
  {
    key: 'windows',
    name: 'Windows',
    status: 'available',
    note: 'Captures native app audio via WASAPI loopback + your microphone. Requires Windows 10 or 11.',
  },
  {
    key: 'ios',
    name: 'iPhone / iPad',
    status: 'considering',
    note: 'ReplayKit / broadcast-upload capture. Under consideration.',
  },
  { key: 'android', name: 'Android', status: 'considering', note: 'AudioPlaybackCapture API. Under consideration.' },
];

const AVAILABLE_KEYS = PLATFORMS.filter((p) => p.status === 'available').map((p) => p.key);

// Badge label + color per platform status (avoids nested ternaries in JSX).
const STATUS_BADGE = {
  available: { color: 'green', label: 'Available' },
  considering: { color: 'grey', label: 'Under consideration' },
};

// Audio Capture App vs Virtual Participant — an honest side-by-side so users
// pick the right tool. "app" = this Audio Capture App; "vp" = Virtual Participant.
const COMPARISON = [
  {
    dimension: 'How it captures',
    app: 'Runs on your computer; captures OS system audio + your mic locally.',
    vp: 'A headless bot joins the meeting in the cloud and captures from inside it.',
  },
  {
    dimension: 'Meeting platforms',
    app: 'Any native or web app that plays audio on your computer (Zoom, Teams, Webex, Slack, phone bridges, etc.).',
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
    app: 'You must be in the meeting with the app running on your computer.',
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

// macOS install + usage + troubleshooting. Kept at module scope so React sees a
// stable component type (react/no-unstable-nested-components) and so only the
// selected platform's block is mounted.
const MacInstall = ({ zipName, copyToClipboard }) => (
  <>
    <Container header={<Header variant="h2">Install on macOS</Header>}>
      <SpaceBetween size="m">
        <Box variant="p">
          The app is distributed as source that you build on your Mac with a single script. (A native macOS app using
          ScreenCaptureKit cannot be prebuilt by the LMA cloud pipeline, and Apple&apos;s signing tools are macOS-only,
          so building locally is both required and the most trustworthy option &mdash; nothing to un-quarantine.)
        </Box>
        <ol>
          <li>
            <Box variant="p">
              Click <strong>Download for macOS</strong> above to get <code>{zipName}</code>, then unzip it.
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
              In Terminal, <code>cd</code> into the unzipped folder and run the installer with{' '}
              <strong>
                <code>bash install-macos.sh</code>
              </strong>{' '}
              <Button
                variant="inline-icon"
                iconName="copy"
                ariaLabel="Copy bash install-macos.sh"
                onClick={() => copyToClipboard('bash install-macos.sh')}
              />
              . It checks prerequisites, builds the app, and installs it to{' '}
              <code>/Applications/LMAAudioClient.app</code>. (Terminal is only used to build/install &mdash; you
              won&apos;t run the app from Terminal.)
            </Box>
            <Alert type="info" header="Run it with “bash”, not “./install-macos.sh”">
              On recent macOS, launching a freshly downloaded script directly (<code>./install-macos.sh</code>) is
              blocked by Gatekeeper with <em>“Apple could not verify … is free of malware.”</em> Running{' '}
              <code>bash install-macos.sh</code> sidesteps that &mdash; the script then clears the download flag from
              the rest of the folder itself. (Or clear it first with the command in Troubleshooting below.)
            </Alert>
          </li>
          <li>
            <Box variant="p">
              <strong>Launch it like a normal app.</strong> Press <strong>⌘-Space</strong> (Spotlight), type{' '}
              <strong>LMA Audio Client</strong> and press Return (or double-click it in Finder). An <strong>LMA</strong>{' '}
              item appears in the menu bar (top-right).
            </Box>
            <Alert type="warning" header="Don't launch it from Terminal">
              Always launch via Spotlight, Finder, or <code>open -a &quot;LMA Audio Client&quot;</code> &mdash; never
              the binary inside <code>Contents/MacOS</code>. Only launching through macOS gives the app its own privacy
              identity; running it from Terminal makes macOS attribute Microphone / Screen Recording to{' '}
              <strong>Terminal</strong>, and system-audio capture silently won&apos;t work (you&apos;ll see
              &quot;Terminal&quot; where the app should be).
            </Alert>
          </li>
          <li>
            <Box variant="p">
              Approve the <strong>Microphone</strong> prompt. Then open{' '}
              <strong>System Settings &rsaquo; Privacy &amp; Security &rsaquo; Screen Recording</strong>, enable{' '}
              <strong>LMA Audio Client</strong>, then <strong>quit and relaunch</strong> it (right-click the LMA
              menu-bar item &rsaquo; Quit, then reopen via Spotlight) &mdash; Screen Recording only takes effect after a
              relaunch. Screen Recording is what lets macOS capture system/meeting audio.
            </Box>
          </li>
          <li>
            <Box variant="p">
              Left-click the <strong>LMA</strong> menu-bar item, sign in with your LMA username and password, and click{' '}
              <strong>Start</strong>. Your meeting appears in the <Link href="#/calls">Meetings List</Link> with a live
              transcript.
            </Box>
          </li>
        </ol>
        <Alert type="info" header="Tip: use headphones">
          For the cleanest transcript, wear headphones. Otherwise your speakers&apos; meeting audio can bleed into your
          microphone and appear faintly on both transcript channels.
        </Alert>
      </SpaceBetween>
    </Container>

    <Container header={<Header variant="h2">Running it in the background (macOS)</Header>}>
      <SpaceBetween size="s">
        <Box variant="p">
          The app is a menu-bar app (no Dock icon). It uses no audio or CPU when idle, so the intended usage is to leave
          it running and click <strong>Start</strong> when a meeting begins.
        </Box>
        <ul>
          <li>
            <strong>Left-click</strong> the <strong>LMA</strong> menu-bar item (top-right) for controls.{' '}
            <strong>Right-click</strong> it for <strong>Quit</strong>.
          </li>
          <li>
            <strong>Start automatically at login:</strong> turn on the login toggle in the popover (or System Settings
            &rsaquo; General &rsaquo; Login Items). The installer already placed the app in <code>/Applications</code>,
            so this works out of the box.
          </li>
          <li>
            <strong>Launch or relaunch:</strong> press <strong>⌘-Space</strong>, type <strong>LMA Audio Client</strong>,
            and press Return &mdash; or run <code>open -a &quot;LMA Audio Client&quot;</code>.
          </li>
        </ul>
      </SpaceBetween>
    </Container>

    <ExpandableSection headerText="Troubleshooting (macOS)">
      <SpaceBetween size="s">
        <Box variant="p">
          <strong>&quot;Apple could not verify … is free of malware&quot; (Gatekeeper).</strong> macOS flags files
          downloaded from a browser, and on recent versions it blocks running a downloaded script <em>directly</em> (
          <code>./install-macos.sh</code>). Run it as <code>bash install-macos.sh</code> instead &mdash; that isn&apos;t
          blocked, and the script clears the flag from the rest of the folder. To clear the whole folder up front
          instead, run: <code>xattr -dr com.apple.quarantine .</code>
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
          <code>bash install-macos.sh</code>.
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
  </>
);

// Windows install + usage + troubleshooting. WASAPI loopback needs no OS
// permission, so the only OS gate is the microphone privacy toggle.
const WindowsInstall = ({ zipName, copyToClipboard }) => (
  <>
    <Container header={<Header variant="h2">Install on Windows</Header>}>
      <SpaceBetween size="m">
        <Box variant="p">
          The app is distributed as source that you build on your PC with a single script. (A native Windows app using
          WASAPI/WPF cannot be prebuilt by the LMA cloud Linux pipeline, and code signing is Windows-only, so building
          locally is both required and the most trustworthy option.)
        </Box>
        <Alert type="success" header="No system-audio permission needed">
          On Windows, capturing system (loopback) audio is built in and needs <strong>no special permission</strong>.
          The only OS gate is the microphone privacy setting, below.
        </Alert>
        <ol>
          <li>
            <Box variant="p">
              Click <strong>Download for Windows</strong> above to get <code>{zipName}</code>, then unzip it (right-click
              &rsaquo; <strong>Extract All</strong>).
            </Box>
          </li>
          <li>
            <Box variant="p">
              Install the <strong>.NET 8 SDK</strong> once from{' '}
              <Link href="https://dotnet.microsoft.com/download/dotnet/8.0" external target="_blank">
                dotnet.microsoft.com
              </Link>{' '}
              (no admin needed &mdash; it can install into your user folder).
            </Box>
          </li>
          <li>
            <Box variant="p">
              In PowerShell, <code>cd</code> into the unzipped folder and run the build script:{' '}
              <strong>
                <code>./build-windows.ps1 -SelfContained</code>
              </strong>{' '}
              <Button
                variant="inline-icon"
                iconName="copy"
                ariaLabel="Copy build-windows.ps1 command"
                onClick={() => copyToClipboard('./build-windows.ps1 -SelfContained')}
              />
              . It builds a standalone <code>LMAAudioClient.exe</code> (nothing else to install) and runs a built-in
              self-test.
            </Box>
          </li>
          <li>
            <Box variant="p">
              <strong>Launch it</strong> by double-clicking <code>LMAAudioClient.exe</code> in the{' '}
              <code>...\publish\</code> folder. An <strong>LMA</strong> icon appears in the system tray (bottom-right
              notification area). If SmartScreen warns about an unrecognized app, choose <strong>More info ▸ Run
              anyway</strong> (expected for a locally built, unsigned app).
            </Box>
          </li>
          <li>
            <Box variant="p">
              If Windows blocks microphone access, enable it in{' '}
              <strong>Settings &rsaquo; Privacy &amp; security &rsaquo; Microphone</strong> (turn on{' '}
              <em>Microphone access</em> and <em>Let desktop apps access your microphone</em>), then restart the app.
              System/meeting audio needs no permission.
            </Box>
          </li>
          <li>
            <Box variant="p">
              Left-click the <strong>LMA</strong> tray icon, sign in with your LMA username and password, and click{' '}
              <strong>Start</strong>. Your meeting appears in the <Link href="#/calls">Meetings List</Link> with a live
              transcript.
            </Box>
          </li>
        </ol>
        <Alert type="info" header="Tip: use headphones">
          For the cleanest transcript, wear headphones. Otherwise your speakers&apos; meeting audio can bleed into your
          microphone and appear faintly on both transcript channels.
        </Alert>
      </SpaceBetween>
    </Container>

    <Container header={<Header variant="h2">Running it in the background (Windows)</Header>}>
      <SpaceBetween size="s">
        <Box variant="p">
          The app is a system-tray app (no taskbar button when idle). It uses no audio or CPU when idle, so the intended
          usage is to leave it running and click <strong>Start</strong> when a meeting begins.
        </Box>
        <ul>
          <li>
            <strong>Left-click</strong> the <strong>LMA</strong> tray icon for controls.{' '}
            <strong>Right-click</strong> it for <strong>Quit</strong>.
          </li>
          <li>
            <strong>Start automatically at login:</strong> turn on the login toggle in the panel &mdash; it adds a
            per-user startup entry that launches the tray app when you sign in.
          </li>
          <li>
            <strong>Remember my email</strong> prefills your login next launch (email only; the password is never
            stored).
          </li>
        </ul>
      </SpaceBetween>
    </Container>

    <ExpandableSection headerText="Troubleshooting (Windows)">
      <SpaceBetween size="s">
        <Box variant="p">
          <strong>&quot;Windows protected your PC&quot; (SmartScreen).</strong> Expected for a locally built, unsigned
          app. Click <strong>More info</strong>, then <strong>Run anyway</strong>.
        </Box>
        <Box variant="p">
          <strong>No microphone / &quot;access denied&quot;.</strong> Enable{' '}
          <strong>Settings &rsaquo; Privacy &amp; security &rsaquo; Microphone</strong> (both <em>Microphone access</em>{' '}
          and <em>Let desktop apps access your microphone</em>), then restart the app. System audio is unaffected.
        </Box>
        <Box variant="p">
          <strong>&quot;dotnet is not recognized&quot; or build errors.</strong> Install the{' '}
          <Link href="https://dotnet.microsoft.com/download/dotnet/8.0" external target="_blank">
            .NET 8 SDK
          </Link>
          , open a fresh PowerShell window, and re-run <code>./build-windows.ps1 -SelfContained</code>.
        </Box>
        <Box variant="p">
          <strong>No remote-participant audio in the transcript.</strong> Make sure meeting audio is actually playing
          through your default playback device (the app captures the default render endpoint). Switching the default
          device mid-meeting is handled automatically.
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
  </>
);

const installPropTypes = {
  zipName: PropTypes.string.isRequired,
  copyToClipboard: PropTypes.func.isRequired,
};
MacInstall.propTypes = installPropTypes;
WindowsInstall.propTypes = installPropTypes;

const AudioCaptureApp = () => {
  const { settings } = useSettingsContext() || {};
  const version = useMemo(() => resolveVersion(settings), [settings]);
  const detected = useMemo(() => detectOS(), []);

  // The platform selector drives ALL OS-specific content (download button +
  // install steps + how-it-works + background + troubleshooting), so the page
  // shows one platform at a time instead of a long homogeneous wall. It defaults
  // to the detected OS when that OS is available, else macOS.
  const defaultPlatform = AVAILABLE_KEYS.includes(detected) ? detected : 'mac';
  const [platform, setPlatform] = useState(defaultPlatform);

  // Download URLs are published by the audio-capture-app stack under the
  // conventional versioned filename served from the web root. The "-macos" /
  // "-windows" segment selects the per-platform package the CodeBuild job emits.
  const zipName = (osKey) => {
    const base = `lma-audio-capture-app-${osKey}`;
    return version ? `${base}-${version}.zip` : `${base}.zip`;
  };
  const macDownloadHref = settings?.AudioCaptureAppDownloadUrl || `/${zipName('macos')}`;
  const winDownloadHref = settings?.AudioCaptureAppWindowsDownloadUrl || `/${zipName('windows')}`;

  const isMac = platform === 'mac';
  const osZipName = isMac ? zipName('macos') : zipName('windows');
  const osDownloadHref = isMac ? macDownloadHref : winDownloadHref;
  const osLabel = isMac ? 'macOS' : 'Windows';
  // Only apply the download filename hint for the web-root fallback (not for an
  // absolute settings URL, which may be cross-origin).
  const osDownloadIsFallback = isMac
    ? !settings?.AudioCaptureAppDownloadUrl
    : !settings?.AudioCaptureAppWindowsDownloadUrl;

  const copyToClipboard = (text) => {
    try {
      navigator.clipboard.writeText(text);
    } catch (e) {
      // no-op
    }
  };

  const downloadButtonText = version ? `Download for ${osLabel} (${version})` : `Download for ${osLabel}`;

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
                href={osDownloadHref}
                download={osDownloadIsFallback ? osZipName : undefined}
                target="_blank"
                rel="noopener noreferrer"
              >
                {downloadButtonText}
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

      <Container
        header={
          <Header variant="h2" description="macOS and Windows are available today; mobile is under consideration.">
            Choose your platform
          </Header>
        }
      >
        <SpaceBetween size="m">
          <SegmentedControl
            selectedId={platform}
            onChange={({ detail }) => setPlatform(detail.selectedId)}
            label="Select your operating system"
            options={[
              { id: 'mac', text: 'macOS' },
              { id: 'windows', text: 'Windows' },
              { id: 'mobile', text: 'Mobile', disabled: true },
            ]}
          />
          {PLATFORMS.filter((p) => p.key === platform).map((p) => (
            <Box key={p.key}>
              <SpaceBetween direction="horizontal" size="s">
                <Box variant="strong">{p.name}</Box>
                <Badge color={STATUS_BADGE[p.status].color}>{STATUS_BADGE[p.status].label}</Badge>
                {p.key === detected && <Badge color="blue">Your system</Badge>}
              </SpaceBetween>
              <Box variant="small" color="text-body-secondary">
                {p.note}
              </Box>
            </Box>
          ))}
          {detected !== 'mac' && detected !== 'windows' && (
            <Alert type="info" header="Desktop only today">
              You appear to be on a mobile or unrecognized system. The Audio Capture App is available for{' '}
              <strong>macOS and Windows</strong>; mobile is under consideration. On this device, use the{' '}
              <Link href={`#${STREAM_AUDIO_PATH}`}>Stream Audio</Link> page or the{' '}
              <Link href={`#${VIRTUAL_PARTICIPANT_PATH}`}>Virtual Participant</Link>.
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      {isMac ? (
        <MacInstall zipName={osZipName} copyToClipboard={copyToClipboard} />
      ) : (
        <WindowsInstall zipName={osZipName} copyToClipboard={copyToClipboard} />
      )}

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
              {isMac ? (
                <>
                  The app lives in your <strong>menu bar</strong>; the icon turns red while recording. Click Stop to end
                  the meeting, which then finalizes in your Meetings List.
                </>
              ) : (
                <>
                  The app lives in your <strong>system tray</strong> (notification area); the icon turns red while
                  recording. Click Stop to end the meeting, which then finalizes in your Meetings List.
                </>
              )}
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
    </SpaceBetween>
  );
};

export default AudioCaptureApp;
