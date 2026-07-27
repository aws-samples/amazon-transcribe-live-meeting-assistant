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

// What the Windows tray icon actually looks like, so users know what to hunt for
// in the notification area (the app opens no window, which surprises people).
// Drawn inline as SVG rather than shipping a screenshot: it stays crisp at any
// zoom, needs no build asset, and the colors are the same ones IconFactory.Make()
// uses in the app (idle #535B66, recording #D42A2A).
const TrayGlyph = ({ recording }) => (
  <svg width="34" height="34" viewBox="0 0 32 32" role="img" aria-label={recording ? 'Recording' : 'Idle'}>
    <circle cx="16" cy="16" r="15" fill={recording ? '#D42A2A' : '#535B66'} />
    {recording ? (
      <circle cx="16" cy="16" r="6" fill="#FFFFFF" />
    ) : (
      [6, 12, 9, 14, 7].map((h, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <rect key={i} x={7 + i * 4.5} y={(32 - h) / 2} width="3" height={h} fill="#FFFFFF" />
      ))
    )}
  </svg>
);
TrayGlyph.propTypes = { recording: PropTypes.bool };
TrayGlyph.defaultProps = { recording: false };

// Compact "this is the icon, in both states" strip used in the Windows steps.
const TrayIconLegend = () => (
  <Box padding={{ top: 'xxs', bottom: 'xxs' }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: '24px', flexWrap: 'wrap' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <TrayGlyph />
        <Box variant="span" fontSize="body-s" color="text-body-secondary">
          idle
        </Box>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <TrayGlyph recording />
        <Box variant="span" fontSize="body-s" color="text-body-secondary">
          recording
        </Box>
      </div>
    </div>
  </Box>
);

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
        <ol>
          <li>
            <Box variant="p">
              Click <strong>Download for macOS</strong> above to get <code>{zipName}</code>, then double-click it to
              unzip.
            </Box>
          </li>
          <li>
            <Box variant="p">
              Open <strong>Terminal</strong>, then copy and run these two commands one at a time. The first installs
              Apple&apos;s build tools (skip if you already have them); the second builds and installs the app.
            </Box>
            <Box variant="code">
              xcode-select --install{' '}
              <Button
                variant="inline-icon"
                iconName="copy"
                ariaLabel="Copy xcode-select --install"
                onClick={() => copyToClipboard('xcode-select --install')}
              />
            </Box>
            <Box variant="code">
              cd ~/Downloads/{zipName.replace('.zip', '')} && bash install-macos.sh{' '}
              <Button
                variant="inline-icon"
                iconName="copy"
                ariaLabel="Copy install command"
                onClick={() =>
                  copyToClipboard(`cd ~/Downloads/${zipName.replace('.zip', '')} && bash install-macos.sh`)
                }
              />
            </Box>
          </li>
          <li>
            <Box variant="p">
              <strong>Open the app</strong> from the <strong>Dock</strong> (the installer adds it there) or from
              Spotlight (<strong>⌘-Space</strong>, type <strong>LMA Audio Client</strong>). An <strong>LMA</strong> icon
              appears in the menu bar at the top-right.
            </Box>
          </li>
          <li>
            <Box variant="p">
              Approve the <strong>Microphone</strong> prompt. Then go to <strong>System Settings</strong> &rsaquo;{' '}
              <strong>Privacy &amp; Security</strong> &rsaquo; <strong>Screen Recording</strong>, turn on{' '}
              <strong>LMA Audio Client</strong>, and <strong>quit and reopen the app</strong> (this permission only
              takes effect after a restart). This is what lets it hear the other participants.
            </Box>
          </li>
          <li>
            <Box variant="p">
              Click the <strong>LMA</strong> menu-bar icon, sign in with your LMA email and password, and click{' '}
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
          The app lives in the <strong>menu bar</strong> and the <strong>Dock</strong>. It uses no audio or CPU when
          idle, so the intended usage is to leave it running and click <strong>Start</strong> when a meeting begins.
        </Box>
        <ul>
          <li>
            <strong>Left-click</strong> the <strong>LMA</strong> menu-bar item (top-right) for controls.{' '}
            <strong>Right-click</strong> it for <strong>Quit</strong>.
          </li>
          <li>
            <strong>Dock icon:</strong> shows a <strong>red dot + REC badge</strong> while recording (⏸ if paused).{' '}
            <strong>Right-click</strong> it for Start / Pause / Stop; <strong>click</strong> it to open the control
            panel as a window. Useful on notched MacBooks, where a crowded menu bar can hide the LMA icon &mdash;
            especially when recording starts and the system&apos;s orange mic indicator appears.
          </li>
          <li>
            <strong>Start automatically at login:</strong> turn on the login toggle in the popover (or System Settings
            &rsaquo; General &rsaquo; Login Items). The installer already placed the app in <code>/Applications</code>,
            so this works out of the box.
          </li>
          <li>
            <strong>Settings (⚙ gear):</strong> set the transcript <strong>speaker labels</strong> for each channel and
            pick a specific <strong>microphone</strong> (or leave System Default). Each label field shows its default in
            grey &mdash; your email for the mic, &quot;Other participants&quot; for system audio &mdash; so leave it
            blank to accept that, or type to override. Changes apply to your next recording.
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
          <strong>Uninstall.</strong> From the unzipped folder, run <code>bash install-macos.sh --uninstall</code>{' '}
          <Button
            variant="inline-icon"
            iconName="copy"
            ariaLabel="Copy uninstall command"
            onClick={() => copyToClipboard('bash install-macos.sh --uninstall')}
          />
          . It removes the app from <code>/Applications</code>, the Start-at-login item, and the Dock pin, and clears
          the app&apos;s saved settings and Screen Recording / Microphone permissions.
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
        <ol>
          <li>
            <Box variant="p">
              Click <strong>Download for Windows</strong> above to get <code>{zipName}</code>, then right-click it
              &rsaquo; <strong>Extract All</strong>.
            </Box>
          </li>
          <li>
            <Box variant="p">
              Install the <strong>.NET 8 SDK</strong> once from{' '}
              <Link href="https://dotnet.microsoft.com/download/dotnet/8.0" external target="_blank">
                dotnet.microsoft.com
              </Link>{' '}
              (no admin needed).
            </Box>
          </li>
          <li>
            <Box variant="p">
              Open <strong>PowerShell</strong>, then copy and run this command to build and install the app (replace the
              path with where you extracted the folder):
            </Box>
            <Box variant="code">
              cd $HOME\Downloads\{zipName.replace('.zip', '')}; ./build-windows.ps1 -SelfContained -Install{' '}
              <Button
                variant="inline-icon"
                iconName="copy"
                ariaLabel="Copy build-and-install command"
                onClick={() =>
                  copyToClipboard(
                    `cd $HOME\\Downloads\\${zipName.replace('.zip', '')}; ./build-windows.ps1 -SelfContained -Install`,
                  )
                }
              />
            </Box>
            <Box variant="p" fontSize="body-s" color="text-body-secondary">
              If SmartScreen warns about an unrecognized app, choose <strong>More info &rsaquo; Run anyway</strong>.
            </Box>
          </li>
          <li>
            <Box variant="p">
              <strong>Open the app</strong> from the <strong>Start Menu</strong> (press the <strong>Windows key</strong>
              , type <strong>LMA Audio Capture</strong>, Enter). No window opens &mdash; look for this icon in the
              system tray at the bottom-right, next to the clock, and <strong>left-click</strong> it:
            </Box>
            <TrayIconLegend />
          </li>
          <li>
            <Box variant="p">
              If Windows blocks the microphone, go to <strong>Settings</strong> &rsaquo;{' '}
              <strong>Privacy &amp; security</strong> &rsaquo; <strong>Microphone</strong>, turn on{' '}
              <em>Microphone access</em> and <em>Let desktop apps access your microphone</em>, then restart the app. (No
              permission is needed for the other participants&apos; audio.)
            </Box>
          </li>
          <li>
            <Box variant="p">
              Left-click the <strong>LMA</strong> tray icon, sign in with your LMA email and password, and click{' '}
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
          The app lives in the system tray, with no taskbar button when idle. It uses no audio or CPU when idle, so the
          intended usage is to leave it running and click <strong>Start</strong> when a meeting begins.
        </Box>
        <ul>
          <li>
            <strong>Left-click</strong> the <strong>LMA</strong> tray icon for controls. <strong>Right-click</strong> it
            for <strong>Quit</strong>. The icon turns <strong>red</strong> while recording.
          </li>
          <li>
            <strong>While recording you also get a taskbar button</strong> (it appears on Start and disappears on Stop).
            Its icon shows a <strong>red dot</strong>, and it glows green while recording / yellow while paused. Hover
            it for <strong>Pause/Resume</strong> and <strong>Stop</strong> buttons, right-click for quick actions, or
            click it to open the controls in a window. Closing that window keeps recording &mdash; only{' '}
            <strong>Stop</strong> stops it.
          </li>
          <li>
            <strong>Keep it one click away when idle:</strong> right-click <strong>LMA Audio Capture</strong> in the
            Start Menu &rsaquo; <strong>More</strong> &rsaquo; <strong>Pin to taskbar</strong>. (Windows 10+ removed the
            API that would let the installer pin it for you.)
          </li>
          <li>
            <strong>Only one copy runs at a time.</strong> Opening the app again &mdash; from that pinned shortcut, the
            Start Menu, or the .exe &mdash; opens the controls panel of the copy already running, rather than adding a
            second tray icon.
          </li>
          <li>
            <strong>Start automatically at login:</strong> turn on the login toggle in the panel &mdash; it adds a
            per-user startup entry that launches the tray app when you sign in.
          </li>
          <li>
            <strong>Remember my email</strong> prefills your login next launch (email only; the password is never
            stored).
          </li>
          <li>
            <strong>Settings (⚙ gear):</strong> set the transcript <strong>speaker labels</strong> for each channel and
            pick a specific <strong>microphone</strong> (or leave System Default). Each label field shows its default in
            grey &mdash; your email for the mic, &quot;Other participants&quot; for system audio &mdash; so leave it
            blank to accept that, or type to override. Changes apply to your next recording.
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
          <strong>Unfamiliar errors scroll past during install (e.g. &quot;log4net:ERROR&quot;).</strong> If the script
          ends with <strong>INSTALL SUCCEEDED</strong>, the install worked. Messages like{' '}
          <code>log4net:ERROR ... lockingModel</code> come from other software already on your PC (corporate sync,
          backup, or security tools that plug into Windows Explorer), not from LMA &mdash; the app doesn&apos;t use
          log4net. The signals that matter are <strong>All self-tests PASSED</strong> and{' '}
          <strong>INSTALL SUCCEEDED</strong>.
        </Box>
        <Box variant="p">
          <strong>&quot;dotnet is not recognized&quot; or build errors.</strong> Install the{' '}
          <Link href="https://dotnet.microsoft.com/download/dotnet/8.0" external target="_blank">
            .NET 8 SDK
          </Link>
          , open a fresh PowerShell window, and re-run <code>./build-windows.ps1 -SelfContained -Install</code>.
        </Box>
        <Box variant="p">
          <strong>Uninstall.</strong> The app registers in{' '}
          <strong>Settings &rsaquo; Apps &rsaquo; Installed apps</strong> as &quot;LMA Audio Capture&quot; &mdash; find
          it there and choose <strong>Uninstall</strong>. Or, from the unzipped folder, run{' '}
          <code>./build-windows.ps1 -Uninstall</code>{' '}
          <Button
            variant="inline-icon"
            iconName="copy"
            ariaLabel="Copy uninstall command"
            onClick={() => copyToClipboard('./build-windows.ps1 -Uninstall')}
          />
          . Either way it removes the installed app and its Start Menu / Desktop shortcuts and clears the app&apos;s
          per-user settings (remembered email, start-at-login). If you installed machine-wide with{' '}
          <code>-ProgramFiles</code>, run the uninstall from an elevated (admin) PowerShell.
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
                  The app lives in your <strong>menu bar</strong> and <strong>Dock</strong>; both icons show red while
                  recording. Click Stop to end the meeting, which then finalizes in your Meetings List.
                </>
              ) : (
                <>
                  The app lives in your <strong>system tray</strong> (notification area); the icon turns red while
                  recording, and a <strong>taskbar button</strong> appears for as long as the recording runs. Click Stop
                  to end the meeting, which then finalizes in your Meetings List.
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
