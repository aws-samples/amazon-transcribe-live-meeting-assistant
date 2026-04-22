/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useMemo } from 'react';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  ExpandableSection,
  Header,
  Icon,
  Link,
  SpaceBetween,
} from '@cloudscape-design/components';

import { LMA_VERSION } from '../common/constants';
import useSettingsContext from '../../contexts/settings';
import { STREAM_AUDIO_PATH, VIRTUAL_PARTICIPANT_PATH } from '../../routes/constants';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const detectIsChromium = () => {
  try {
    if (typeof navigator === 'undefined') return true;
    // userAgentData is most reliable when available (Chromium-based browsers)
    const uaData = navigator.userAgentData;
    if (uaData && Array.isArray(uaData.brands)) {
      return uaData.brands.some((b) => /chromium|google chrome|microsoft edge|brave|opera/i.test(b.brand));
    }
    const ua = navigator.userAgent || '';
    return /Chrome|Chromium|Edg\//.test(ua) && !/OPR\//.test(ua) === false ? true : /Chrome|Chromium|Edg\//.test(ua);
  } catch (e) {
    return true;
  }
};

const resolveVersion = (settings) => {
  // Prefer SSM runtime value (populated at deploy time); fall back to the
  // build-time LMA_VERSION constant. If neither has been substituted, return null
  // so the UI can hide version-specific strings instead of displaying the raw token.
  const raw = settings?.Version || LMA_VERSION;
  if (!raw || raw.includes('<VERSION_TOKEN>')) return null;
  return raw.startsWith('v') ? raw : `v${raw}`;
};

const BrowserExtension = () => {
  const { settings } = useSettingsContext() || {};
  const version = useMemo(() => resolveVersion(settings), [settings]);
  const zipFileName = version ? `lma-chrome-extension-${version}.zip` : 'lma-chrome-extension.zip';
  const zipHref = `/${zipFileName}`;
  const isChromium = useMemo(() => detectIsChromium(), []);

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
            description="Install the LMA extension to transcribe meetings from inside your browser tab."
            actions={
              <Button
                variant="primary"
                iconName="download"
                href={zipHref}
                download={zipFileName}
                target="_blank"
                rel="noopener noreferrer"
              >
                {version ? `Download extension (${version})` : 'Download extension'}
              </Button>
            }
          >
            Chrome Extension
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            The LMA Chrome extension adds a side-panel to meeting tabs on popular platforms &mdash; Zoom, Microsoft
            Teams, Amazon Chime, Cisco Webex, and Google Meet &mdash; so you can start and stop transcription with a
            single click. It captures both your microphone and the other participants&apos; audio from the tab, without
            adding a bot or extra attendee to the meeting.
          </Box>
          <Alert type="info" header="Requires joining the meeting from your Chrome browser">
            The extension can only capture audio from a meeting that is running inside a browser tab. You must join the
            meeting from the meeting platform&apos;s web client in Chrome (or another Chromium-based browser) &mdash;
            the extension cannot see or capture audio from native desktop or mobile meeting apps.
          </Alert>
          {!isChromium && (
            <Alert type="warning" header="Chromium-based browser recommended">
              This extension is packaged for Chrome and other Chromium-based browsers (Microsoft Edge, Brave, Arc). It
              won&apos;t install on Firefox or Safari. If you&apos;re on a non-Chromium browser, use{' '}
              <Link href={`#${STREAM_AUDIO_PATH}`}>Stream Audio (from Mic+Browser)</Link> or{' '}
              <Link href={`#${VIRTUAL_PARTICIPANT_PATH}`}>Virtual Participant</Link> instead.
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Which option is right for me?</Header>}>
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Chrome Extension</Box>
            <SpaceBetween size="xxs">
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                Runs inside meeting tab
              </div>
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                Captures both sides of audio
              </div>
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                Speaker attribution from meeting platform
              </div>
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                Invisible to other attendees
              </div>
              <div>
                <Box variant="span" color="text-status-info">
                  <Icon name="status-info" />
                </Box>{' '}
                Requires joining meeting from Chrome browser
              </div>
              <div>
                <Box variant="span" color="text-status-info">
                  <Icon name="status-info" />
                </Box>{' '}
                One-time install (Chromium browsers only)
              </div>
            </SpaceBetween>
          </div>
          <div>
            <Box variant="awsui-key-label">
              <Link href={`#${STREAM_AUDIO_PATH}`}>Stream Audio (from Mic+Browser)</Link>
            </Box>
            <SpaceBetween size="xxs">
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                No install required
              </div>
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                Works with any streaming audio in a Chrome tab &mdash; meetings, softphones, YouTube, audio/video
                playback
              </div>
              <div>
                <Box variant="span" color="text-status-info">
                  <Icon name="status-info" />
                </Box>{' '}
                Separate LMA tab (not inside meeting)
              </div>
              <div>
                <Box variant="span" color="text-status-info">
                  <Icon name="status-info" />
                </Box>{' '}
                No speaker attribution (no access to meeting app metadata)
              </div>
            </SpaceBetween>
          </div>
          <div>
            <Box variant="awsui-key-label">
              <Link href={`#${VIRTUAL_PARTICIPANT_PATH}`}>Virtual Participant</Link>
            </Box>
            <SpaceBetween size="xxs">
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                No install required
              </div>
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                Works even when you&apos;re offline
              </div>
              <div>
                <Box variant="span" color="text-status-success">
                  <Icon name="status-positive" />
                </Box>{' '}
                Supports the Voice Assistant and &quot;Open VP live view&quot; in the Meeting Assistant
              </div>
              <div>
                <Box variant="span" color="text-status-info">
                  <Icon name="status-info" />
                </Box>{' '}
                Joins meeting as a visible bot attendee
              </div>
            </SpaceBetween>
          </div>
        </ColumnLayout>
      </Container>

      <Container header={<Header variant="h2">Install the extension</Header>}>
        <SpaceBetween size="m">
          <Box variant="p">
            Follow these one-time steps to load the extension into Chrome (or any Chromium-based browser).
          </Box>
          <ol>
            <li>
              <Box variant="p">
                Click the <strong>Download extension</strong> button above to download <code>{zipFileName}</code>.
              </Box>
            </li>
            <li>
              <Box variant="p">
                Unzip the file to a local folder. You should end up with a folder named{' '}
                <code>lma-chrome-extension</code> (or similar) containing a <code>manifest.json</code> file.
              </Box>
            </li>
            <li>
              <Box variant="p">
                Open a new browser tab and navigate to <code>chrome://extensions</code>{' '}
                <Button
                  variant="inline-icon"
                  iconName="copy"
                  ariaLabel="Copy chrome://extensions to clipboard"
                  onClick={() => copyToClipboard('chrome://extensions')}
                />
                . Chrome blocks clicking that link directly &mdash; paste it into the address bar.
              </Box>
            </li>
            <li>
              <Box variant="p">
                Toggle <strong>Developer mode</strong> on (top-right of the extensions page).
              </Box>
            </li>
            <li>
              <Box variant="p">
                Click <strong>Load unpacked</strong> and select the unzipped <code>lma-chrome-extension</code> folder.
              </Box>
            </li>
            <li>
              <Box variant="p">
                Pin the <strong>Amazon Live Meeting Assistant</strong> extension to your toolbar for easy access (click
                the puzzle-piece icon, then the pin next to the extension).
              </Box>
            </li>
            <li>
              <Box variant="p">Click the extension icon and sign in with your LMA credentials.</Box>
            </li>
          </ol>
          <Box>
            <Link href={`${DOCS_BASE}/browser-extension/`} external target="_blank">
              View full installation guide with screenshots
            </Link>
          </Box>
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">How to use</Header>}>
        <SpaceBetween size="s">
          <ol>
            <li>Open your meeting in Zoom, Teams, Chime, Webex, or Google Meet in a browser tab.</li>
            <li>
              Click the LMA extension icon in the toolbar. The side panel shows a <strong>Start Listening</strong>{' '}
              button.
            </li>
            <li>
              Click <strong>Start Listening</strong> and choose <strong>Allow</strong> when prompted to share the
              browser tab audio.
            </li>
            <li>
              Click <strong>Open in LMA</strong> to view the live transcript, select a translation language, and chat
              with the Meeting Assistant.
            </li>
            <li>
              When the meeting ends, click <strong>Stop Listening</strong>. The meeting appears in your Meetings List.
            </li>
          </ol>
        </SpaceBetween>
      </Container>

      <ExpandableSection headerText="Troubleshooting">
        <SpaceBetween size="s">
          <Box variant="p">
            <strong>The extension does not appear after Load unpacked.</strong> Make sure you selected the unzipped{' '}
            folder itself (which contains <code>manifest.json</code>), not the zip file or a parent directory.
          </Box>
          <Box variant="p">
            <strong>Sign-in fails.</strong> Use the same email/password you use to log in to this LMA web app. If your
            organization uses SSO, complete sign-in in a normal browser tab first, then re-open the extension.
          </Box>
          <Box variant="p">
            <strong>After deploying a new LMA version, the extension stops working.</strong> Download the latest
            extension zip from this page and re-load it. The version appears on the Download button above.
          </Box>
          <Box variant="p">
            For more help, see the{' '}
            <Link href={`${DOCS_BASE}/browser-extension/`} external target="_blank">
              Browser Extension documentation
            </Link>
            .
          </Box>
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  );
};

export default BrowserExtension;
