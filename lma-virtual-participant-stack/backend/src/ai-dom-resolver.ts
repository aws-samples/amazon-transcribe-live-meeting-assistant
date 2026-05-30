/* eslint-disable @typescript-eslint/no-explicit-any */
import { Page, ElementHandle } from 'playwright-core';
import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';
import {
  DynamoDBClient,
  GetItemCommand,
  PutItemCommand,
  DeleteItemCommand,
} from '@aws-sdk/client-dynamodb';
import { marshall, unmarshall } from '@aws-sdk/util-dynamodb';

export type Platform = 'ZOOM' | 'TEAMS' | 'WEBEX' | 'CHIME';

export interface ResolveOptions {
  intent: string;
  platform: Platform;
  step: string;
  useScreenshot?: boolean;
}

export interface RetryOptions {
  maxRetries: number;
  delayMs: number;
}

export interface FindResult {
  element: ElementHandle<Element>;
  selector: string;
  source: 'primary' | 'cache' | 'ai';
}

export type DialogType =
  | 'CONSENT'
  | 'RECORDING_NOTICE'
  | 'CAPTCHA'
  | 'SSO_REDIRECT'
  | 'LOGIN_REQUIRED'
  | 'BLOCKED'
  | 'OTHER';

export interface DialogAnalysis {
  type: DialogType;
  primaryActionSelector?: string;
  message: string;
  needsHuman: boolean;
}

/**
 * Vision-enabled page-level navigation decision. Used by the post-login
 * navigator: instead of running a hardcoded regex over visible link text,
 * Claude looks at the full page (screenshot + interactive-element snapshot)
 * and tells us what action to take.
 *
 * Possible decisions:
 *   - 'skip'        — found a Skip / Not now / Maybe later / Cancel link.
 *                     selector is set to the dismissive control. Click it.
 *   - 'continue'    — page is just informational and has a single primary
 *                     "Continue" / "Next" / "Got it" button that doesn't
 *                     enroll us in anything. selector is set. Click it.
 *   - 'wait'        — page is loading / transient / no actionable element
 *                     yet. Sleep and re-evaluate.
 *   - 'needs_human' — page requires real human input (CAPTCHA, OTP, 2FA,
 *                     SSO redirect, blocked-by-bot-detection, etc.).
 *                     reason explains why; escalate to MANUAL_ACTION_REQUIRED.
 *   - 'done'        — we've reached an authenticated/destination page
 *                     (Zoom home, account, my-meetings, web client, the
 *                     meeting URL itself). Stop navigating.
 */
export type PageActionKind =
  | 'skip'
  | 'continue'
  | 'wait'
  | 'needs_human'
  | 'done'
  | 'fill_password';

export interface PageAction {
  kind: PageActionKind;
  selector?: string;
  // For fill_password: also give the submit button selector so we can
  // type into `selector` then click `submitSelector`.
  submitSelector?: string;
  reason: string;
}

interface CacheEntry {
  selector: string;
  modelId: string;
  discoveredAt: string;
  lastUsedAt: string;
  lastVerifiedAt: string;
  hits: number;
  misses: number;
  expiresAt: number;
}

interface InteractiveElement {
  tag: string;
  id?: string;
  name?: string;
  role?: string;
  ariaLabel?: string;
  type?: string;
  placeholder?: string;
  text?: string;
  classes?: string;
  dataset?: Record<string, string>;
  bbox?: { x: number; y: number; w: number; h: number };
}

const DEFAULT_MODEL_ID =
  process.env.BEDROCK_DOM_RESOLVER_MODEL_ID ||
  'us.anthropic.claude-haiku-4-5-20251001-v1:0';
const RESOLVER_DISABLED = !DEFAULT_MODEL_ID;
const TABLE_NAME = process.env.DOM_SELECTOR_CACHE_TABLE_NAME || '';
const AWS_REGION = process.env.AWS_REGION || 'us-east-1';
const TTL_DAYS = 30;

const memoryCache = new Map<string, CacheEntry>();

// Race a browser-call promise against a wall-clock timeout. evaluate /
// screenshot / boundingBox can hang for tens of seconds when the page's
// execution context is destroyed mid-call (Zoom SPA re-mount); this turns
// the hang into a fast rejection so the surrounding retry loop can fire.
const withCdpTimeout = async <T>(
  promise: Promise<T>,
  ms: number,
  label: string,
): Promise<T> => {
  let to: NodeJS.Timeout | undefined;
  try {
    return await Promise.race<T>([
      promise,
      new Promise<T>((_, reject) => {
        to = setTimeout(
          () => reject(new Error(`[ai-dom-resolver] ${label} timed out after ${ms}ms`)),
          ms,
        );
      }),
    ]);
  } finally {
    if (to) clearTimeout(to);
  }
};

let bedrockClient: BedrockRuntimeClient | null = null;
let ddbClient: DynamoDBClient | null = null;

const getBedrock = (): BedrockRuntimeClient => {
  if (!bedrockClient) {
    bedrockClient = new BedrockRuntimeClient({ region: AWS_REGION });
  }
  return bedrockClient;
};

const getDdb = (): DynamoDBClient | null => {
  if (!TABLE_NAME) return null;
  if (!ddbClient) {
    ddbClient = new DynamoDBClient({ region: AWS_REGION });
  }
  return ddbClient;
};

const cacheKey = (platform: Platform, step: string): string =>
  `${platform}#${step}`;

const ttlEpoch = (): number =>
  Math.floor(Date.now() / 1000) + TTL_DAYS * 24 * 60 * 60;

export async function getCachedSelector(
  platform: Platform,
  step: string,
): Promise<CacheEntry | null> {
  const key = cacheKey(platform, step);
  const mem = memoryCache.get(key);
  if (mem) return mem;

  const ddb = getDdb();
  if (!ddb) return null;

  try {
    const result = await ddb.send(
      new GetItemCommand({
        TableName: TABLE_NAME,
        Key: marshall({ pk: key }),
      }),
    );
    if (!result.Item) return null;
    const entry = unmarshall(result.Item) as CacheEntry;
    if (entry.expiresAt && entry.expiresAt < Math.floor(Date.now() / 1000)) {
      return null;
    }
    memoryCache.set(key, entry);
    return entry;
  } catch (err) {
    console.warn(`[ai-dom-resolver] Cache GetItem failed for ${key}:`, err);
    return null;
  }
}

async function persistCacheEntry(
  platform: Platform,
  step: string,
  entry: CacheEntry,
): Promise<void> {
  const key = cacheKey(platform, step);
  memoryCache.set(key, entry);
  const ddb = getDdb();
  if (!ddb) return;
  try {
    await ddb.send(
      new PutItemCommand({
        TableName: TABLE_NAME,
        Item: marshall({ pk: key, ...entry }, { removeUndefinedValues: true }),
      }),
    );
  } catch (err) {
    console.warn(`[ai-dom-resolver] Cache PutItem failed for ${key}:`, err);
  }
}

async function evictCacheEntry(
  platform: Platform,
  step: string,
): Promise<void> {
  const key = cacheKey(platform, step);
  memoryCache.delete(key);
  const ddb = getDdb();
  if (!ddb) return;
  try {
    await ddb.send(
      new DeleteItemCommand({
        TableName: TABLE_NAME,
        Key: marshall({ pk: key }),
      }),
    );
  } catch (err) {
    console.warn(`[ai-dom-resolver] Cache DeleteItem failed for ${key}:`, err);
  }
}

/**
 * Resolve a CSS selector to ElementHandles. Tolerates Claude (and human-doc)
 * habit of producing jQuery `:contains('text')` extensions, which the browser
 * doesn't understand — falls back to an in-page text scan with the prefix
 * selector. Returns at most one element when the `:contains()` form is used,
 * matching the jQuery semantics callers expect.
 */
async function querySelectorAllSafe(
  page: Page,
  selector: string,
): Promise<ElementHandle<Element>[]> {
  const containsMatch = selector.match(/^(.*?):contains\((['"])(.+?)\2\)\s*$/);
  if (containsMatch) {
    const baseSelector = containsMatch[1].trim() || '*';
    const wantText = containsMatch[3].toLowerCase();
    const handle = (await withCdpTimeout(
      page.evaluateHandle(
        ({ sel, text }: { sel: string; text: string }) => {
          const candidates = Array.from(document.querySelectorAll(sel));
          for (const el of candidates) {
            const t = (el.textContent || '').trim().toLowerCase();
            if (t.includes(text)) {
              const rect = (el as HTMLElement).getBoundingClientRect();
              if (rect.width > 0 && rect.height > 0) return el;
            }
          }
          return null;
        },
        { sel: baseSelector, text: wantText },
      ),
      2000,
      `evaluateHandle(${selector})`,
    )) as ElementHandle<Element> | null;
    const isNull = await withCdpTimeout(
      page.evaluate((h) => h === null, handle as any),
      2000,
      'evaluate(isNull)',
    );
    if (isNull) {
      await (handle as any).dispose();
      return [];
    }
    return [handle as ElementHandle<Element>];
  }
  return withCdpTimeout(page.$$(selector), 2000, `page.$$(${selector})`);
}

async function selectorMatches(
  page: Page,
  selector: string,
): Promise<ElementHandle<Element> | null> {
  // Wrap boundingBox in a per-call timeout — the underlying CDP call has
  // been observed to hang on destroyed execution contexts. See
  // `withCdpTimeout` for context.
  const safeBox = (h: ElementHandle<Element>) =>
    withCdpTimeout(h.boundingBox(), 2000, 'boundingBox').catch(() => null);
  try {
    const handles = await querySelectorAllSafe(page, selector);
    if (handles.length !== 1) {
      // Allow multiple matches if exactly one is visible
      let visibleHandle: ElementHandle<Element> | null = null;
      for (const h of handles) {
        const box = await safeBox(h);
        if (box && box.width > 0 && box.height > 0) {
          if (visibleHandle) {
            return null;
          }
          visibleHandle = h;
        }
      }
      return visibleHandle;
    }
    const box = await safeBox(handles[0]);
    if (!box || box.width === 0 || box.height === 0) return null;
    return handles[0];
  } catch {
    return null;
  }
}

async function snapshotInteractiveElements(
  page: Page,
  maxElements = 80,
): Promise<InteractiveElement[]> {
  return withCdpTimeout(
    page.evaluate((max: number) => {
    const out: any[] = [];
    const sels = [
      'button',
      'input',
      'select',
      'textarea',
      'a[href]',
      '[role="button"]',
      '[role="dialog"]',
      '[role="alertdialog"]',
      '[role="textbox"]',
      '[contenteditable="true"]',
    ];
    const set = new Set<Element>();
    for (const s of sels) {
      document.querySelectorAll(s).forEach((el) => set.add(el));
    }
    const truncate = (s: string | null | undefined, n: number): string =>
      (s || '').replace(/\s+/g, ' ').trim().slice(0, n);

    for (const el of Array.from(set)) {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;
      const style = window.getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none') continue;

      const dataset: Record<string, string> = {};
      const ds = (el as HTMLElement).dataset || {};
      for (const k of Object.keys(ds)) {
        const v = ds[k];
        if (v) dataset[k] = truncate(v, 60);
      }
      out.push({
        tag: el.tagName.toLowerCase(),
        id: (el as HTMLElement).id || undefined,
        name: (el as HTMLInputElement).name || undefined,
        role: el.getAttribute('role') || undefined,
        ariaLabel: el.getAttribute('aria-label') || undefined,
        type: (el as HTMLInputElement).type || undefined,
        placeholder: (el as HTMLInputElement).placeholder || undefined,
        text: truncate(el.textContent, 80),
        classes: truncate((el as HTMLElement).className?.toString?.(), 120),
        dataset: Object.keys(dataset).length ? dataset : undefined,
        bbox: {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
        },
      });
      if (out.length >= max) break;
    }
    return out;
  }, maxElements),
    3000,
    'snapshotInteractiveElements',
  );
}

async function snapshotVisibleDialogs(page: Page): Promise<{ html: string }[]> {
  return withCdpTimeout(
    page.evaluate(() => {
      const candidates = Array.from(
        document.querySelectorAll(
          '[role="dialog"],[role="alertdialog"],.zm-modal,.zm-modal-legacy,.ReactModal__Content',
        ),
      );
      const out: { html: string }[] = [];
      for (const el of candidates) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        const html = (el as HTMLElement).outerHTML
          .replace(/\s+/g, ' ')
          .slice(0, 4000);
        out.push({ html });
      }
      return out;
    }),
    3000,
    'snapshotVisibleDialogs',
  );
}

async function captureScreenshot(page: Page): Promise<string | null> {
  try {
    // Playwright's screenshot() always returns a Buffer (no `encoding` option).
    const buf = await withCdpTimeout(
      page.screenshot({
        type: 'png',
        fullPage: false,
        clip: { x: 0, y: 0, width: 1024, height: 768 },
      }),
      5000,
      'captureScreenshot',
    );
    return buf.toString('base64');
  } catch {
    return null;
  }
}

interface ClaudeResponse {
  content: { type: string; text?: string }[];
}

async function invokeClaude(
  prompt: string,
  screenshotB64?: string | null,
): Promise<string> {
  const messages: any[] = [];
  const userContent: any[] = [];
  if (screenshotB64) {
    userContent.push({
      type: 'image',
      source: {
        type: 'base64',
        media_type: 'image/png',
        data: screenshotB64,
      },
    });
  }
  userContent.push({ type: 'text', text: prompt });
  messages.push({ role: 'user', content: userContent });

  const body = {
    anthropic_version: 'bedrock-2023-05-31',
    max_tokens: 600,
    temperature: 0,
    messages,
  };

  const command = new InvokeModelCommand({
    modelId: DEFAULT_MODEL_ID,
    body: JSON.stringify(body),
    contentType: 'application/json',
    accept: 'application/json',
  });
  const response = await getBedrock().send(command);
  const decoded = new TextDecoder().decode(response.body);
  const parsed = JSON.parse(decoded) as ClaudeResponse;
  const text = parsed.content
    ?.filter((c) => c.type === 'text')
    .map((c) => c.text || '')
    .join('\n')
    .trim();
  return text || '';
}

function extractJson(text: string): any | null {
  if (!text) return null;
  // Strip markdown code-fence wrappers Claude sometimes produces.
  let cleaned = text.trim();
  // ```json ... ``` or ``` ... ```
  const fenceMatch = cleaned.match(/^```(?:json)?\s*\n?([\s\S]*?)\n?```\s*$/);
  if (fenceMatch) {
    cleaned = fenceMatch[1].trim();
  }
  // Find the largest {...} block that successfully parses. We prefer the
  // outermost braces but fall back to any inner block if the outer fails.
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start === -1 || end === -1 || end <= start) return null;
  // Try the outermost first.
  try {
    return JSON.parse(cleaned.slice(start, end + 1));
  } catch {
    // If that fails, walk inner braces — handles cases where the model
    // emits prose after a valid JSON object.
    let depth = 0;
    let firstOpen = -1;
    for (let i = start; i <= end; i++) {
      const c = cleaned[i];
      if (c === '{') {
        if (depth === 0) firstOpen = i;
        depth += 1;
      } else if (c === '}') {
        depth -= 1;
        if (depth === 0 && firstOpen !== -1) {
          try {
            return JSON.parse(cleaned.slice(firstOpen, i + 1));
          } catch {
            // keep walking
          }
        }
      }
    }
    return null;
  }
}

const RESOLVE_PROMPT = (
  opts: ResolveOptions,
  elems: InteractiveElement[],
  primarySelectors: string[],
  previousAttempt?: { selector: string; reason: string },
): string => {
  const parts: string[] = [];
  parts.push(
    `You are helping a meeting bot interact with the ${opts.platform} web client.`,
  );
  parts.push(`Goal: locate the DOM element for: ${opts.intent}`);
  parts.push(`Step key: ${opts.step}`);
  if (primarySelectors.length) {
    parts.push(
      `Existing primary selectors that did NOT match: ${primarySelectors.join(', ')}`,
    );
  }
  if (previousAttempt) {
    parts.push(
      `Your previous attempt selector "${previousAttempt.selector}" was rejected because: ${previousAttempt.reason}`,
    );
  }
  parts.push(
    'Below is a snapshot of visible interactive elements on the page (JSON array).',
  );
  parts.push('```json');
  parts.push(JSON.stringify(elems));
  parts.push('```');
  parts.push(
    'Return ONLY a raw JSON object (no prose, no markdown code fences): {"selector": "...", "reason": "..."}.',
  );
  parts.push(
    'The selector MUST be a single CSS selector that uniquely matches the target element on this page right now.',
  );
  parts.push(
    'Prefer stable attributes (id, data-*, aria-label, role, name, type) over class chains.',
  );
  parts.push(
    'If no element on the page matches the goal, return {"selector": null, "reason": "..."}.',
  );
  return parts.join('\n');
};

const DIALOG_PROMPT = (platform: Platform, dialogs: { html: string }[]): string => {
  return [
    `You are reviewing modal dialogs that appeared on the ${platform} web client during an automated meeting join.`,
    'For each dialog you must classify it and decide whether to auto-dismiss or escalate to a human.',
    '',
    'Visible dialog HTML (truncated):',
    '```html',
    dialogs.map((d) => d.html).join('\n---\n'),
    '```',
    '',
    'Return ONLY a raw JSON object (no prose, no markdown code fences) with these fields:',
    '{',
    '  "type": "CONSENT" | "RECORDING_NOTICE" | "CAPTCHA" | "SSO_REDIRECT" | "LOGIN_REQUIRED" | "BLOCKED" | "OTHER",',
    '  "primaryActionSelector": "<CSS selector for the button to click to dismiss/accept, or null if a human is needed>",',
    '  "message": "<one sentence describing the dialog for a human user>",',
    '  "needsHuman": <true if this requires manual user action (CAPTCHA / 2FA / SSO / blocked), else false>',
    '}',
    '',
    'CONSENT and RECORDING_NOTICE should always have a primaryActionSelector and needsHuman=false.',
    'CAPTCHA, SSO_REDIRECT, LOGIN_REQUIRED, BLOCKED should set needsHuman=true.',
    'If unsure, return type=OTHER, needsHuman=true.',
  ].join('\n');
};

export async function resolveSelector(
  page: Page,
  opts: ResolveOptions,
  primarySelectors: string[] = [],
): Promise<string | null> {
  if (RESOLVER_DISABLED) {
    console.log('[ai-dom-resolver] Resolver disabled (no model id) — skipping');
    return null;
  }
  // Try cache
  const cached = await getCachedSelector(opts.platform, opts.step);
  if (cached) {
    const matched = await selectorMatches(page, cached.selector);
    if (matched) {
      await persistCacheEntry(opts.platform, opts.step, {
        ...cached,
        hits: cached.hits + 1,
        lastUsedAt: new Date().toISOString(),
        lastVerifiedAt: new Date().toISOString(),
        expiresAt: ttlEpoch(),
      });
      return cached.selector;
    }
    console.log(
      `[ai-dom-resolver] Cached selector "${cached.selector}" no longer matches — evicting and re-resolving`,
    );
    await evictCacheEntry(opts.platform, opts.step);
  }

  let lastAttempt: { selector: string; reason: string } | undefined;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const elems = await snapshotInteractiveElements(page);
      const screenshot =
        opts.useScreenshot && attempt === 0
          ? await captureScreenshot(page)
          : null;
      const prompt = RESOLVE_PROMPT(opts, elems, primarySelectors, lastAttempt);
      const text = await invokeClaude(prompt, screenshot);
      const json = extractJson(text);
      if (!json || !json.selector) {
        console.log(
          `[ai-dom-resolver] Claude returned no selector for ${opts.step}: ${text.slice(0, 200)}`,
        );
        return null;
      }
      const selector: string = json.selector;
      const matched = await selectorMatches(page, selector);
      if (matched) {
        const now = new Date().toISOString();
        await persistCacheEntry(opts.platform, opts.step, {
          selector,
          modelId: DEFAULT_MODEL_ID,
          discoveredAt: now,
          lastUsedAt: now,
          lastVerifiedAt: now,
          hits: 1,
          misses: 0,
          expiresAt: ttlEpoch(),
        });
        console.log(
          `[ai-dom-resolver] Discovered selector for ${opts.step}: ${selector}`,
        );
        return selector;
      }
      lastAttempt = {
        selector,
        reason:
          'no element matched, or multiple matched without one visible — try a more specific selector',
      };
    } catch (err) {
      console.error('[ai-dom-resolver] Bedrock call failed:', err);
      return null;
    }
  }
  return null;
}

export async function findElementWithFallback(
  page: Page,
  primarySelectors: string[],
  opts: ResolveOptions,
  retry: RetryOptions = { maxRetries: 10, delayMs: 500 },
): Promise<FindResult | null> {
  for (let attempt = 1; attempt <= retry.maxRetries; attempt++) {
    for (const selector of primarySelectors) {
      const handle = await selectorMatches(page, selector);
      if (handle) {
        return { element: handle, selector, source: 'primary' };
      }
    }
    if (attempt < retry.maxRetries) {
      await new Promise((r) => setTimeout(r, retry.delayMs));
    }
  }
  // Fallback to AI
  const aiSelector = await resolveSelector(page, opts, primarySelectors);
  if (!aiSelector) return null;
  const handle = await selectorMatches(page, aiSelector);
  if (!handle) return null;
  return {
    element: handle,
    selector: aiSelector,
    source: (await getCachedSelector(opts.platform, opts.step))
      ? 'cache'
      : 'ai',
  };
}

export async function analyzeUnknownDialog(
  page: Page,
  opts: { platform: Platform },
): Promise<DialogAnalysis | null> {
  if (RESOLVER_DISABLED) return null;
  const dialogs = await snapshotVisibleDialogs(page);
  if (!dialogs.length) return null;
  try {
    const text = await invokeClaude(DIALOG_PROMPT(opts.platform, dialogs));
    const json = extractJson(text);
    if (!json || !json.type) return null;
    const result: DialogAnalysis = {
      type: json.type,
      primaryActionSelector:
        typeof json.primaryActionSelector === 'string' &&
        json.primaryActionSelector
          ? json.primaryActionSelector
          : undefined,
      message: typeof json.message === 'string' ? json.message : '',
      needsHuman: !!json.needsHuman,
    };
    return result;
  } catch (err) {
    console.error('[ai-dom-resolver] Dialog analysis failed:', err);
    return null;
  }
}

const PAGE_ACTION_PROMPT = (
  platform: Platform,
  url: string,
  elems: InteractiveElement[],
  opts: { allowFillPassword: boolean },
): string => {
  const kindList = opts.allowFillPassword
    ? '"fill_password" | "skip" | "continue" | "wait" | "needs_human" | "done"'
    : '"skip" | "continue" | "wait" | "needs_human" | "done"';
  const fillRule = opts.allowFillPassword
    ? '- "fill_password" → page is showing a password input field as part of normal sign-in flow. Set "selector" to the password input and "submitSelector" to the submit/sign-in button. Do NOT pick this if the page is asking for a One-Time Passcode, verification code, or 2FA code (those go to "needs_human").\n'
    : '';
  return [
    `You are guiding an automated meeting bot through ${platform}'s sign-in flow.`,
    `Current URL: ${url}`,
    '',
    'IMPORTANT: The bot DOES have the user\'s username AND password (supplied via',
    'AWS Secrets Manager — the human user pre-stored them). It is allowed and',
    'expected to type the password and click the Sign-in button. A visible password',
    `input on a ${platform} sign-in page is NOT a "needs_human" situation. The bot`,
    'only needs human help for things it cannot have: CAPTCHA, OTP/verification',
    'codes, 2FA codes, SSO/IdP redirects, bot-detection blocks, account locked.',
    '',
    'CONTEXT: The bot has typed an email and clicked "Next". It is now on a',
    'page that may be one of:',
    opts.allowFillPassword
      ? '  (z) The password-entry step of the normal sign-in flow — a visible password input plus a Sign In button. The bot has the password; pick "fill_password".'
      : '  (z) The password-entry page AGAIN (the bot already typed the password, so fill_password is no longer offered). If the password input still has dots in it and a Sign In button is visible, pick "continue" with the Sign In button as the selector — submission may not have fired yet. If the page shows "incorrect password" or similar text, the credentials are bad and the bot will fail-fast on its own; pick "wait".',
    '  (a) A "skip-able" upsell — passkey enrollment, phone/SMS binding, "stay signed in?",',
    '      browser-extension promo, marketing page. These have a "Skip for now" /',
    '      "Not now" / "Maybe later" / "Cancel" / "Don\'t ask again" / "I\'ll do this later" link.',
    '  (b) An informational page with a single safe "Continue" / "Next" / "Got it" button',
    '      that just acknowledges the page (does NOT enroll/add/enable anything).',
    '  (c) A page that genuinely needs the human user — CAPTCHA, OTP / one-time passcode,',
    '      2FA, SSO redirect, bot-detection block, account-locked. The bot CANNOT proceed.',
    '  (d) A loading / transient state with no actionable element yet.',
    '  (e) The destination — authenticated home page, the meeting itself, etc.',
    '',
    'YOUR TASK: Pick the safest action. NEVER click "Add", "Enable", "Set up", "Allow",',
    '"Save password", "Continue with Google/Apple", or any positive enrollment action.',
    'When in doubt between skip/continue and needs_human, prefer the dismissive option.',
    'OTP / verification-code entry is ALWAYS needs_human (the bot does not have the code).',
    'A plain password-entry page is NEVER needs_human (the bot has the password).',
    '',
    'Visible interactive elements on the page (JSON):',
    '```json',
    JSON.stringify(elems),
    '```',
    '',
    'Return ONLY a raw JSON object (no prose, no markdown fences):',
    '{',
    `  "kind": ${kindList},`,
    '  "selector": "<CSS selector for the element to click — required for skip/continue/fill_password, omit otherwise>",',
    opts.allowFillPassword
      ? '  "submitSelector": "<CSS selector for the submit button — required for fill_password, omit otherwise>",'
      : '',
    '  "reason": "<one sentence explaining what this page is and why this action is safe>"',
    '}',
    '',
    'Rules:',
    fillRule,
    '- "skip" → an explicit dismissive link/button is visible. Pick it; never pick a positive action.',
    '- "continue" → the page is purely informational with a single safe acknowledgement button.',
    '- "wait" → loading spinner / no real action available yet.',
    '- "needs_human" → CAPTCHA, OTP entry, 2FA code, SSO redirect, blocked. selector omitted.',
    '- "done" → we\'re on an authenticated destination page (zoom.us/profile, /myaccount,',
    '  /wc/, /meeting, the meeting itself). selector omitted.',
    '- The selector MUST be unique on the current page and refer to a visible, clickable element.',
    '- Use only standard CSS selectors. Do NOT use jQuery extensions like `:contains()`,',
    '  `:has-text()`, or `:visible` — they are not supported by the browser engine.',
    '  To pick a button by its label, use the `data-testid`, `id`, `name`, `aria-label`,',
    '  or `class` attributes shown in the JSON snapshot above. If multiple buttons match',
    '  by class, prefer the one whose `text` field contains the relevant label.',
  ].filter(Boolean).join('\n');
};

/**
 * Vision + DOM-snapshot-based decision-maker for an unknown page.
 *
 * Used by the post-login navigator. Captures both a low-res screenshot and
 * the interactive-element snapshot, sends them to Claude, and asks for a
 * structured action recommendation. Robust against page redesigns: if Zoom
 * ships a new "bind-X" interstitial tomorrow, this still works without code
 * changes — we just need the page to have a visible "Skip"-equivalent
 * (which Zoom virtually always provides).
 */
export async function analyzePageAction(
  page: Page,
  opts: {
    platform: Platform;
    useScreenshot?: boolean;
    allowFillPassword?: boolean;
  },
): Promise<PageAction | null> {
  if (RESOLVER_DISABLED) return null;
  try {
    const url = page.url();
    const elems = await snapshotInteractiveElements(page);
    const screenshot =
      opts.useScreenshot !== false ? await captureScreenshot(page) : null;
    const prompt = PAGE_ACTION_PROMPT(opts.platform, url, elems, {
      allowFillPassword: !!opts.allowFillPassword,
    });
    const text = await invokeClaude(prompt, screenshot);
    const json = extractJson(text);
    if (!json || !json.kind) return null;
    const kind = json.kind as PageActionKind;
    const valid = ['skip', 'continue', 'wait', 'needs_human', 'done'];
    if (opts.allowFillPassword) valid.push('fill_password');
    if (!valid.includes(kind)) {
      return null;
    }
    return {
      kind,
      selector:
        typeof json.selector === 'string' && json.selector
          ? json.selector
          : undefined,
      submitSelector:
        typeof json.submitSelector === 'string' && json.submitSelector
          ? json.submitSelector
          : undefined,
      reason: typeof json.reason === 'string' ? json.reason : '',
    };
  } catch (err) {
    console.error('[ai-dom-resolver] analyzePageAction failed:', err);
    return null;
  }
}

/**
 * Scroll an element into view, then click it. Useful when an AI-resolved
 * selector points at something the rendered viewport hasn't actually
 * scrolled to (e.g. Zoom's right-column form on a wide layout). Falls
 * back to a normal page.click if the scroll-then-click flow fails.
 */
export async function scrollIntoViewAndClick(
  page: Page,
  selector: string,
): Promise<boolean> {
  try {
    const handles = await querySelectorAllSafe(page, selector);
    const handle = handles[0] || null;
    if (!handle) return false;
    try {
      await handle.evaluate((el: Element) => {
        (el as HTMLElement).scrollIntoView({
          behavior: 'instant' as ScrollBehavior,
          block: 'center',
          inline: 'center',
        });
      });
      // brief pause for any layout transitions
      await new Promise((r) => setTimeout(r, 150));
    } catch {
      // ignore — scroll is best-effort
    }
    try {
      await handle.click();
      return true;
    } catch {
      // fallback: dispatch a synthetic click on the element. Works even
      // when the positional click misses due to overlays.
      try {
        await handle.evaluate((el: Element) => (el as HTMLElement).click());
        return true;
      } catch {
        return false;
      }
    }
  } catch {
    return false;
  }
}

export function isResolverEnabled(): boolean {
  return !RESOLVER_DISABLED;
}
