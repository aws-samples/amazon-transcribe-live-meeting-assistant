// Copyright (c) 2025 Amazon.com
// This file is licensed under the MIT-0 License.
// See the LICENSE file in the project root for full license information.
//
// A scripted guest in a Google Meet meeting, for demo recordings and the
// scheduled end-to-end test. It runs as a one-off ECS task from the Virtual
// Participant's own container image (see meeting_guest.py), so it joins from
// AWS with the same CloakBrowser the VP uses, and it has no AWS credentials:
// everything it reads or writes goes through presigned URLs.
//
// Environment:
//   MEET_URL    the meeting link
//   GUEST_NAME  the name typed on the pre-join screen
//   AUDIO_URL   presigned GET of a WAV file, played once as the microphone
//   CMDS_URL    presigned GET of the commands file, polled every few seconds
//   SHOT_URL    presigned PUT for the latest screenshot (optional)
//
// Each line of the commands file runs once, in order:
//   chat <text>  send <text> in the meeting chat
//   present      share a tab of synthetic slides
//   shot         upload a screenshot to SHOT_URL
//   leave        leave the meeting and exit
//
// The log lines "IN CALL" and "GAVE UP" are read by meeting_guest.py.
import { launchPersistentContext } from 'cloakbrowser';

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), '[guest]', ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const MEET_URL = process.env.MEET_URL.includes('?') ? process.env.MEET_URL : `${process.env.MEET_URL}?hl=en`;
const NAME = process.env.GUEST_NAME || 'Conference Room 4';
const AUDIO = '/tmp/guest-audio.wav';
const MAX_MINUTES = Number(process.env.MAX_MINUTES || 90);

async function download(url, path) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`download failed: HTTP ${r.status}`);
  const { writeFile } = await import('node:fs/promises');
  await writeFile(path, Buffer.from(await r.arrayBuffer()));
}

async function clickFirst(page, locators, timeout = 4000) {
  for (const l of locators) {
    try {
      await l.first().click({ timeout });
      return true;
    } catch {
      /* try the next one */
    }
  }
  return false;
}

async function shot(page) {
  if (!process.env.SHOT_URL) return log('no SHOT_URL');
  const body = await page.screenshot();
  const r = await fetch(process.env.SHOT_URL, { method: 'PUT', body, headers: { 'content-type': 'image/png' } });
  log('shot', r.status);
}

const SLIDES_HTML = `<!doctype html><html><head><title>Slides</title><style>
body{margin:0;font-family:Arial,sans-serif;background:#fff}
.h{background:#232f3e;color:#fff;padding:40px 60px;font-size:56px;font-weight:bold}
ul{font-size:42px;line-height:1.9;margin:50px 80px;color:#232f3e}
</style></head><body><div class="h" id="t"></div><ul id="l"></ul><script>
const S=[["Mobile app launch plan",["Target launch: March 20","Payments screen: done","Offline sync: about 2 weeks left","App store submission by March 10"]],
["Beta feedback",["About 400 beta testers","Top issue: slow sign-in","Fix: optional verification for returning users"]],
["Support hiring",["Two support engineers wanted","Role 1: open this week","Role 2: pending budget approval"]]];
let i=0;function show(){const [t,l]=S[i%S.length];document.getElementById('t').textContent=t;document.getElementById('l').innerHTML=l.map(x=>'<li>'+x+'</li>').join('');}
show();setInterval(()=>{i++;show();},45000);
</script></body></html>`;

await download(process.env.AUDIO_URL, AUDIO);
const context = await launchPersistentContext({
  headless: false,
  humanize: true,
  humanPreset: 'default',
  userDataDir: `/tmp/guest-profile-${process.pid}`,
  viewport: { width: 1280, height: 800 },
  args: [
    `--fingerprint=${Math.floor(Math.random() * 2 ** 31)}`,
    '--fingerprint-screen-width=1280',
    '--fingerprint-screen-height=800',
    '--window-size=1280,800',
    '--window-position=0,0',
    '--use-fake-ui-for-media-stream',
    '--auto-select-tab-capture-source-by-title=Slides',
    '--auto-select-desktop-capture-source=Slides',
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${AUDIO}%noloop`,
    '--autoplay-policy=no-user-gesture-required',
    '--disable-blink-features=AutomationControlled',
    '--disable-notifications',
    '--disable-dev-shm-usage',
    '--no-first-run',
    '--no-default-browser-check',
    '--password-store=basic',
    '--use-mock-keychain',
  ],
  launchOptions: { ignoreDefaultArgs: ['--mute-audio', '--enable-automation'] },
});
const page = context.pages()[0] || (await context.newPage());

// A short warm-up, as the VP does for a fresh profile.
try {
  await page.goto('https://www.google.com/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await sleep(3000);
} catch (e) {
  log('warm-up failed (continuing):', e.message);
}

// Meet sometimes turns an anonymous guest away at first. Retry with backoff,
// clearing google.com cookies from the second retry on, as the VP does.
const refused = () => page.getByText(/can.t join this video call/i).isVisible().catch(() => false);
const BACKOFF = [5000, 10000, 20000, 20000, 20000];
let inCall = false;
for (let attempt = 0; attempt < 6 && !inCall; attempt += 1) {
  if (attempt) {
    log(`retry ${attempt} in ${BACKOFF[attempt - 1] / 1000}s`);
    await sleep(BACKOFF[attempt - 1]);
    if (attempt >= 2) await context.clearCookies({ domain: /(^|\.)google\.com$/ }).catch(() => undefined);
  }
  await page.goto(MEET_URL, { waitUntil: 'domcontentloaded' }).catch(() => undefined);
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => undefined);
  await sleep(3000);
  if (await refused()) {
    log('refused on load');
    continue;
  }
  await clickFirst(page, [page.getByRole('button', { name: /use microphone and camera/i })], 5000);
  await sleep(1500);
  const nameBox = page.getByRole('textbox', { name: /name/i });
  if (!(await nameBox.count())) {
    log('no name field');
    continue;
  }
  await nameBox.first().fill(NAME);
  await clickFirst(page, [page.locator('[aria-label^="Turn off camera" i]')], 3000);
  await sleep(1000);
  const joined = await clickFirst(page, [
    page.getByRole('button', { name: /^Join now$/i }),
    page.getByRole('button', { name: /^Ask to join$/i }),
  ]);
  log(joined ? 'join clicked' : 'no join button');
  for (let i = 0; i < 40; i += 1) {
    await sleep(1500);
    if (await refused()) break;
    if (await page.locator('[aria-label^="Leave call" i]').count()) {
      inCall = true;
      break;
    }
  }
  log(inCall ? 'IN CALL' : 'not admitted');
}
await shot(page).catch(() => undefined);
if (!inCall) {
  log('GAVE UP');
  await context.close();
  process.exit(2);
}

let done = 0;
const deadline = Date.now() + MAX_MINUTES * 60 * 1000;
while (Date.now() < deadline) {
  let lines = [];
  try {
    const r = await fetch(process.env.CMDS_URL);
    if (r.ok) lines = (await r.text()).split('\n').filter(Boolean);
  } catch {
    /* poll again */
  }
  for (; done < lines.length; done += 1) {
    const [cmd, ...rest] = lines[done].split(' ');
    const arg = rest.join(' ');
    try {
      if (cmd === 'chat') {
        const box = page.getByRole('textbox', { name: /send a message/i });
        if (!(await box.count()) || !(await box.first().isVisible())) {
          await clickFirst(page, [
            page.getByRole('button', { name: /chat with everyone/i }),
            page.getByRole('button', { name: /chat/i }),
          ]);
          await sleep(1500);
        }
        await box.first().fill(arg);
        await box.first().press('Enter');
        log('chat sent:', arg);
      } else if (cmd === 'present') {
        const deck = await context.newPage();
        await deck.setContent(SLIDES_HTML);
        await page.bringToFront();
        const ok = await clickFirst(page, [
          page.locator('[aria-label^="Share screen" i]'),
          page.locator('[aria-label*="present" i]'),
        ]);
        await sleep(4000);
        await clickFirst(page, [page.getByRole('button', { name: /share|present/i })], 2000);
        log('present', ok ? 'clicked' : 'button not found');
      } else if (cmd === 'shot') {
        await shot(page);
      } else if (cmd === 'leave') {
        await clickFirst(page, [page.getByRole('button', { name: /leave call/i })]);
        log('left');
        await context.close();
        process.exit(0);
      } else {
        log('unknown command:', cmd);
      }
    } catch (e) {
      log('command failed:', lines[done], e.message.split('\n')[0]);
    }
  }
  await sleep(4000);
}
log('time limit reached; leaving');
await context.close();
process.exit(0);
